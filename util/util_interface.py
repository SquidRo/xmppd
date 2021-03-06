#
# util_interface.py
#
# APIs related to interface.
#

import subprocess, json, pdb, time, re, util_utl, util_method_tbl
from xml.etree import cElementTree as ET

FILL_INFO_NONE  = 0     # fill no info
FILL_INFO_NAME  = 0x01  # fill name info
FILL_INFO_VLAN  = 0x02  # fill vlan mbr info
FILL_INFO_STATE = 0x04  # fill admin/oper info
FILL_INFO_PC    = 0x08  # fill port channel info
FILL_INFO_IP    = 0x10  # fill arp/route info
FILL_INFO_CNTR  = 0x20  # fill counter info
FILL_INFO_PORT  = 0x40  # fill port info
FILL_INFO_ALL   = 0xff  # fill all info

MGMT_PORT_NAME  = 'eth0'


# refer to /usr/bin/intfutil
PORT_STATUS_TABLE_PREFIX = "PORT_TABLE:"
PORT_OPER_STATUS         = "oper_status"
PORT_ADMIN_STATUS        = "admin_status"
PORT_MTU_SIZE            = "mtu"
PORT_SPEED               = "speed"

PORT_LANES_STATUS        = "lanes"
PORT_ALIAS               = "alias"
PORT_DESCRIPTION         = "description"

# refer to /usr/bin/teamshow
PC_STATUS_TABLE_PREFIX   = "LAG_TABLE:"


VLAN_STATUS_TABLE_PREFIX = "VLAN_TABLE:"

# refer to /usr/bin/portstat
COUNTER_TABLE_PREFIX     = "COUNTERS:"
COUNTERS_PORT_NAME_MAP   = "COUNTERS_PORT_NAME_MAP"     # port name / port oid

COUNTERS_QUEUE_PORT_MAP  = "COUNTERS_QUEUE_PORT_MAP"    # queue oid / port oid
COUNTERS_QUEUE_NAME_MAP  = "COUNTERS_QUEUE_NAME_MAP"    # queue name/ queue oid
COUNTERS_QUEUE_INDEX_MAP = "COUNTERS_QUEUE_INDEX_MAP"   # queue oid / queue idx
COUNTERS_QUEUE_TYPE_MAP  = "COUNTERS_QUEUE_TYPE_MAP"    # queue oid / queue type

# for ec only
CFG_TMPL_RLIMIT_PROFILE  = "config qos scheduler {act} {profile}" \
                           " --meter_type {mode} --pir {pir} --pbs {pbs}"
CFG_TMPL_BIND_RLIMIT     = "config interface rate-limit {act} {direction} {infname} {profile}"

CFG_TMPL_PROFILE_FMT     = "{infname}_{stage}_P"

# ex: portq_to_oid['Ethernet1'][4] : oid:0x150000000000e4
portq_to_oid   = {}


#
# set functions
#
def interface_get_rl_pfname(inf_name, stage):
    stage_str = ['EG', 'IN'] [ stage == 'ingress' ]
    pf_name = CFG_TMPL_PROFILE_FMT.format(infname = inf_name, stage = stage_str)
    return pf_name

def interface_set_rl_profile(ent_elm, pf_name, action, rate):
    # mode : 'bytes' or 'packets'
    exe_cmd = CFG_TMPL_RLIMIT_PROFILE.format(
                act=action, profile=pf_name, mode='bytes', pir=rate, pbs=8192)

    exe_ok = util_utl.utl_execute_cmd(exe_cmd)
    if not exe_ok:
        util_method_tbl.mtbl_append_retmsg(ent_elm,
            "Failed to {act} rl profile {profile}.".format(
                act=action, profile=pf_name))

    return exe_ok

def interface_bind_rl_profile(ent_elm, inf_name, pf_name, action, direction):
    exe_cmd = CFG_TMPL_BIND_RLIMIT.format(
                act=action, direction=direction, infname=inf_name, profile=pf_name)

    exe_ok = util_utl.utl_execute_cmd(exe_cmd)
    if not exe_ok:
        util_method_tbl.mtbl_append_retmsg(ent_elm,
            "Failed to {act} {dir} {profile}.".format(
                act=action, dir=direction, profile=pf_name))

    return exe_ok

# ex: <entry><method>port-ratelimit</method><port>Ethernet1</port>
#     <ingress>125000</ingress><egress /></entry>
@util_utl.utl_dbg
def interface_set_rate_limit(ent_elm, db_args):
    port_elm = ent_elm.find('port')
    ing_elm  = ent_elm.find('ingress')
    egr_elm  = ent_elm.find('egress')

    if None in [ port_elm, ing_elm, egr_elm, port_elm.text ]:
        util_method_tbl.mtbl_append_retmsg(ent_elm, 'WRONG PARAMETERS')
    else:
        ing_pf_name = interface_get_rl_pfname(port_elm.text, 'ingress')
        egr_pf_name = interface_get_rl_pfname(port_elm.text, 'egress' )

        is_in_ok = is_eg_ok = True
        if ing_elm.text == None:
            # unbind ingress
            is_in_ok = interface_bind_rl_profile(
                        ent_elm, port_elm.text, ing_pf_name, 'unbind', 'in')
        else:
            # bind ingress
            is_in_ok = interface_set_rl_profile(ent_elm, ing_pf_name, 'add', ing_elm.text)
            is_in_ok = is_in_ok and interface_bind_rl_profile(
                                        ent_elm, port_elm.text, ing_pf_name, 'bind', 'in')

        if egr_elm.text == None:
            # unbind egress
            is_eg_ok = interface_bind_rl_profile(
                        ent_elm, port_elm.text, egr_pf_name, 'unbind', 'out')
        else:
            # bind egress
            is_eg_ok = interface_set_rl_profile(ent_elm, egr_pf_name, 'add', egr_elm.text)
            is_eg_ok = is_eg_ok and interface_bind_rl_profile(
                                        ent_elm, port_elm.text, egr_pf_name, 'bind', 'out')

        if is_in_ok and is_eg_ok:
            util_method_tbl.mtbl_append_retmsg(ent_elm, 'SUCCESS')


#
# get functions
#

def interface_get_queue_port(db, q_oid):
    p_oid = db.get(db.COUNTERS_DB, COUNTERS_QUEUE_PORT_MAP, q_oid)
    return p_oid

def interface_get_queue_idx(db, q_oid):
    q_idx =  db.get(db.COUNTERS_DB, COUNTERS_QUEUE_INDEX_MAP, q_oid)
    return q_idx

def interface_setup_port_queue_map(db_args):
    global portq_to_oid

    if len(portq_to_oid) != 0:
        return

    # ex: port_name_map['oid:0x1000000000011'] : 'Ethernet15'
    port_name_map  = {}

    # ex: {"Ethernet8":"oid:0x100000000000a"}
    cntr_pnmap = db_args.cntrdb.get_all(db_args.cntrdb.COUNTERS_DB, COUNTERS_PORT_NAME_MAP)

    # ex: {'Ethernet10:8': 'oid:0x150000000002c7'}
    cntr_qnmap = db_args.cntrdb.get_all(db_args.cntrdb.COUNTERS_DB, COUNTERS_QUEUE_NAME_MAP)

    for port in cntr_pnmap:
        portq_to_oid [port] = {}
        port_name_map[cntr_pnmap[port]] = port

    for queue in cntr_qnmap:
        p_oid = interface_get_queue_port(db_args.cntrdb, cntr_qnmap[queue])
        q_idx = interface_get_queue_idx(db_args.cntrdb, cntr_qnmap[queue])

        if None in [ p_oid, q_idx ]:
            continue

        portq_to_oid [port_name_map[p_oid]][q_idx] = cntr_qnmap[queue]

# get inf status form appl db
def interface_db_inf_status_get(db, inf_name, status_fld, fill_info):
    pfx_tbl = { FILL_INFO_PC   : PC_STATUS_TABLE_PREFIX,                                                            FILL_INFO_PORT : PORT_STATUS_TABLE_PREFIX,                                                          FILL_INFO_VLAN : VLAN_STATUS_TABLE_PREFIX }

    if fill_info in pfx_tbl:
        pfx = pfx_tbl[fill_info]
    else:                                                                                                   return None

    full_table_id = pfx + inf_name
    status = db.get(db.APPL_DB, full_table_id, status_fld)
    return status

# ex: ret = [ { "idx":"", "name":"", "type":"", "admin":"",
#               "oper":"", "speed":"", "alias":"" } ]
def interface_get_port_info_one(db_args, port_name):
    fld_map = [ {"fld" : "index",           "tag" : "id"   },
                {"fld" : PORT_ADMIN_STATUS, "tag" : "admin"},
                {"fld" : PORT_OPER_STATUS,  "tag" : "oper" },
                {"fld" : PORT_SPEED,        "tag" : "speed"},
                {"fld" : PORT_ALIAS,        "tag" : "alias"} ]

    ret_val = { "name" :  port_name }
    for fld in fld_map:
        val = interface_db_inf_status_get(
                db_args.appdb, port_name, fld["fld"], FILL_INFO_PORT)
        if val and val != "N/A":
            ret_val[fld['tag']] = val
        else:
            ret_val[fld['tag']] = "unknown"

    # TODO: get type from state_db
    #       SFP/SFP+/NA
    # ret_val ['type'] = "N/A"

    return ret_val

def interface_get_port_info(db_args):
    pattern = 'PORT_TABLE:*'

    ret_val = []
    db_keys = db_args.appdb.keys(db_args.appdb.APPL_DB, pattern)
    for i in db_keys:
        inf_name = re.split(':', i, maxsplit=1)[-1].strip()

        if inf_name and inf_name.startswith('Ethernet'):
            ret_one = interface_get_port_info_one(db_args, inf_name)

            ret_val.append(ret_one)

    return ret_val

def interface_get_queue_statis_one(db, inf_name, q_idx):
    fld_map_tbl = [
        { 'fld' : 'SAI_QUEUE_STAT_PACKETS',        'tag' : 'packets'    },
        { 'fld' : 'SAI_QUEUE_STAT_BYTES',          'tag' : 'bytes'      },
        { 'fld' : 'SAI_QUEUE_STAT_DROPPED_PACKETS','tag' : 'dropPackets'},
        { 'fld' : 'SAI_QUEUE_STAT_DROPPED_BYTES',  'tag' : 'dropBytes'  },
    ]

    queu_elm = ET.Element('stats')
    if_elm   = ET.SubElement(queu_elm, 'ifName')
    if_elm.text = inf_name
    qid_elm  = ET.SubElement(queu_elm, 'queue')
    qid_elm.text = q_idx

    if inf_name in portq_to_oid and q_idx in portq_to_oid[inf_name]:
        q_oid = portq_to_oid [inf_name] [q_idx]

        for fld in fld_map_tbl:
            full_table_id = COUNTER_TABLE_PREFIX + q_oid
            tmp_elm = ET.SubElement(queu_elm, fld['tag'])
            data = db.get(db.COUNTERS_DB, full_table_id, fld['fld'])

            if util_utl.CFG_TBL['FAKE_DATA'] != 0:
                data = '1'

            if data != None:
                tmp_elm.text = data

    return queu_elm

def interface_get_queue_statis(ent_elm, db_args):
    interface_setup_port_queue_map(db_args)

    port_elm = ent_elm.find('port')
    queu_elm = ent_elm.find('queue')

    if port_elm == None or port_elm.text == None or port_elm.text not in portq_to_oid:
        util_method_tbl.mtbl_append_retmsg(ent_elm, 'PORT NOT FOUND')
        return

    if queu_elm == None or queu_elm.text == None or queu_elm.text == '':
        # get all queues for a port
        queues_elm = ET.Element('queues')

        for q_idx in portq_to_oid[port_elm.text]:
            ret_one = interface_get_queue_statis_one(db_args.cntrdb, port_elm.text, q_idx)

            queues_elm.append(ret_one)

        ent_elm.append(queues_elm)
    else:
        # get a queue for a port
        ret_one = interface_get_queue_statis_one(db_args.cntrdb, port_elm.text, queu_elm.text)
        ent_elm.append(ret_one)

def interface_get_port_statis_one(db, inf_name, cntr_pname_map):
    fld_map_tbl = [
        { 'fld' : 'SAI_PORT_STAT_IF_IN_OCTETS',         'tag' : 'inOctets'        },
        { 'fld' : 'SAI_PORT_STAT_IF_IN_UCAST_PKTS',     'tag' : 'inUnicastPkts'   },
        { 'fld' : 'SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS', 'tag' : 'inMulticastPkts' },
        { 'fld' : 'SAI_PORT_STAT_IF_IN_BROADCAST_PKTS', 'tag' : 'inBroadcastPkts' },
        { 'fld' : 'SAI_PORT_STAT_IF_IN_ERRORS',         'tag' : 'inErrors'        },
        { 'fld' : 'SAI_PORT_STAT_IF_IN_DISCARDS',       'tag' : 'inDiscards'      },
        { 'fld' : 'SAI_PORT_STAT_IF_IN_UNKNOWN_PROTOS', 'tag' : 'inUnknownProtos' },

        { 'fld' : 'SAI_PORT_STAT_IF_OUT_OCTETS',        'tag' :  'outOctets'      },
        { 'fld' : 'SAI_PORT_STAT_IF_OUT_UCAST_PKTS',    'tag' :  'outUnicastPkts' },
        { 'fld' : 'SAI_PORT_STAT_IF_OUT_NON_UCAST_PKTS','tag' :  'outMulticastPkts' },
        { 'fld' : 'SAI_PORT_STAT_IF_OUT_BROADCAST_PKTS','tag' :  'outBroadcastPkts' },
        { 'fld' : 'SAI_PORT_STAT_IF_OUT_ERRORS',        'tag' :  'outErrors'      },
        { 'fld' : 'SAI_PORT_STAT_IF_OUT_DISCARDS',      'tag' :  'outDiscards'    }
    ]

    port_elm = ET.Element('stats')
    if_elm   = ET.SubElement(port_elm, 'ifName')
    if_elm.text = inf_name

    for fld in fld_map_tbl:
        tmp_elm = ET.SubElement(port_elm, fld['tag'])

        table_id = cntr_pname_map[inf_name]
        full_table_id = COUNTER_TABLE_PREFIX + table_id
        cntr_data =  db.get(db.COUNTERS_DB, full_table_id, fld['fld'])

        tmp_elm.text = cntr_data

        if util_utl.CFG_TBL['FAKE_DATA'] != 0:
            tmp_elm.text = '1'

    return port_elm

def interface_get_port_statis(ent_elm, db_args):
    cntr_pname_map = db_args.cntrdb.get_all(db_args.cntrdb.COUNTERS_DB, COUNTERS_PORT_NAME_MAP)

    port_elm = ent_elm.find('port')
    if port_elm == None or port_elm.text == None or port_elm.text == '':
        # get all ports
        pattern = 'PORT_TABLE:*'

        ports_elm = ET.Element('ports')

        db_keys = db_args.appdb.keys(db_args.appdb.APPL_DB, pattern)
        for i in db_keys:
            inf_name = re.split(':', i, maxsplit=1)[-1].strip()

            if inf_name and inf_name.startswith('Ethernet'):
                ret_one = interface_get_port_statis_one(db_args.cntrdb, inf_name, cntr_pname_map)

                ports_elm.append(ret_one)

        ent_elm.append(ports_elm)
    else:
        # get a port
        ret_one = interface_get_port_statis_one(db_args.cntrdb, port_elm.text, cntr_pname_map)
        ent_elm.append(ret_one)

@util_utl.utl_dbg
def interface_get_statis(ent_elm, db_args):
    type_elm= ent_elm.find('type')
    if type_elm == None or type_elm.text == 'port':
        interface_get_port_statis(ent_elm, db_args)
    else:
        interface_get_queue_statis(ent_elm, db_args)

#
# register related functions to method table
#
util_method_tbl.mtbl_register_method('get-statistics', interface_get_statis)
util_method_tbl.mtbl_register_method('port-ratelimit', interface_set_rate_limit)
