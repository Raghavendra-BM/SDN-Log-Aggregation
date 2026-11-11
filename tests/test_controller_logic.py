"""
Test of the controller LOGIC without Mininet or Ryu: the controller runs against a tiny fake Ryu that records
the flow rules it would send. It checks: routes, queue rules switched on/off by congestion, static mode does
not react, and the queue rules follow a re-route after a link failure.   Run:  python3 tests/test_controller_logic.py
This is NOT a test of real OpenFlow behaviour; tests/smoke_mininet.py does that.
"""
import sys, types, logging, json, os, time
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class Ev:  # fake event classes
    class EventOFPSwitchFeatures: pass
    class EventOFPPortStatus: pass
    class EventOFPPortStatsReply: pass
    class EventOFPFlowStatsReply: pass
def mod(name, **kw):
    m=types.ModuleType(name); m.__dict__.update(kw); sys.modules[name]=m; return m
class RyuApp:
    def __init__(self,*a,**k): self.logger=logging.getLogger("ctl")
spawned=[]
mod('ryu'); mod('ryu.base'); mod('ryu.base.app_manager',RyuApp=RyuApp)
mod('ryu.controller'); mod('ryu.controller.ofp_event',**{k:getattr(Ev,k) for k in dir(Ev) if k.startswith('Event')})
mod('ryu.controller.handler',CONFIG_DISPATCHER=1,MAIN_DISPATCHER=2,set_ev_cls=lambda *a,**k:(lambda f:f))
mod('ryu.lib'); mod('ryu.lib.hub',spawn=lambda f:spawned.append(f),sleep=lambda s:None)
mod('ryu.ofproto'); mod('ryu.ofproto.ofproto_v1_3',OFP_VERSION=4)
sys.modules['ryu'].base=sys.modules['ryu.base']
sys.modules['ryu.controller'].ofp_event=sys.modules['ryu.controller.ofp_event']
sys.modules['ryu.lib'].hub=sys.modules['ryu.lib.hub']
sys.modules['ryu.ofproto'].ofproto_v1_3=sys.modules['ryu.ofproto.ofproto_v1_3']

class Obj:
    def __init__(s,**kw): s.__dict__.update(kw)
class Match(Obj):
    def key(s): return tuple(sorted(s.__dict__.items()))
class Ofp:
    OFPIT_APPLY_ACTIONS=4; OFPFC_ADD=0; OFPFC_DELETE=3; OFPFC_DELETE_STRICT=4; OFP_NO_BUFFER=0xffffffff
    OFPP_ANY=0xffffffff; OFPG_ANY=0xffffffff; OFPTT_ALL=0xff; OFPPS_LINK_DOWN=1; OFPPC_PORT_DOWN=1; OFPPR_DELETE=2
class Parser:
    def OFPMatch(s,**kw): return Match(**kw)
    def OFPActionOutput(s,p,*a): return ('output',p)
    def OFPActionSetQueue(s,q): return ('set_queue',q)
    def OFPInstructionActions(s,t,acts): return Obj(actions=acts)
    def OFPFlowMod(s,**kw): return Obj(kind='flowmod',**kw)
    def OFPPortStatsRequest(s,*a): return Obj(kind='portreq')
    def OFPFlowStatsRequest(s,*a): return Obj(kind='flowreq',a=a)
class DP:
    def __init__(s,i): s.id=i; s.ofproto=Ofp; s.ofproto_parser=Parser(); s.table={}; s.sent=[]
    def send_msg(s,m):
        s.sent.append(m)
        if m.kind!='flowmod': return
        k=(m.priority if hasattr(m,'priority') else 0, m.match.key())
        if m.command==Ofp.OFPFC_ADD: s.table[k]=m
        elif m.command==Ofp.OFPFC_DELETE_STRICT: s.table.pop(k,None)
        else:
            for kk,f in list(s.table.items()):
                if m.cookie_mask==0 and not m.match.key(): s.table.pop(kk)
                elif m.cookie_mask and (f.cookie & m.cookie_mask)==(m.cookie & m.cookie_mask): s.table.pop(kk)

def run(mode):
    os.environ['SDN_MODE']=mode; os.environ['EVENT_LOG']='/tmp/ev_%s.jsonl'%mode
    if os.path.exists(os.environ['EVENT_LOG']): os.remove(os.environ['EVENT_LOG'])
    sys.path.insert(0,ROOT+'/sdn'); sys.path.insert(0,ROOT)
    for m in [m for m in sys.modules if m in('controller','model','common.udp_protocol')]: del sys.modules[m]
    import controller
    c=controller.PriorityController()
    dps={i:DP(i) for i in (1,2,3,4)}
    for d in dps.values():
        c.on_features(Obj(msg=Obj(datapath=d)))
    return c,dps,controller

def queues(dp):
    out={}
    for (prio,mk),f in dp.table.items():
        m=dict(mk)
        if prio==20: out[m['udp_dst']]=f.instructions[0].actions if False else None
    return out
def class_actions(dp):
    res={}
    for (prio,mk),f in dp.table.items():
        if prio==20: res[dict(mk)['udp_dst']]=f.instructions
    return res

def stats_round(c,dps,now,tx):  # tx: {(dpid,port):bytes}
    for dpid,dp in dps.items():
        body=[Obj(port_no=p,tx_bytes=b) for (d,p),b in tx.items() if d==dpid]
        if body:
            import time as t; real=t.time
            t.time=lambda n=now:n
            c.on_port_stats(Obj(msg=Obj(datapath=dp,body=body)))
            t.time=real
    return c

import time as T
for mode in ('dynamic','static'):
    c,dps,ctl=run(mode)
    # routes installed on s1 for server ip
    r10=[dict(mk) for (pr,mk) in dps[1].table if pr==10]
    assert any(m.get('ipv4_dst')=='10.0.0.11' for m in r10), r10
    ca=class_actions(dps[1]); assert set(ca)=={5001,5002,5003}, ca
    def has_q(dp): return any(any(a[0]=='set_queue' for a in i.actions) for ins in class_actions(dp).values() for i in ins)
    assert not has_q(dps[1])
    # quiet rounds, then heavy rounds on s1:5
    tx={(1,5):0}; now=1000.0; real=T.time
    def rnd(mbps):
        global now
        now+=0.5; tx[(1,5)]+=int(mbps*1e6/8*0.5)
        for dp in dps.values():
            T.time=lambda n=now:n
            c.on_port_stats(Obj(msg=Obj(datapath=dp,body=[Obj(port_no=p,tx_bytes=b) for (d,p),b in tx.items() if d==dp.id])))
        T.time=real
        c._evaluate_round()
    rnd(0.3); rnd(0.3)
    assert not has_q(dps[1])
    rnd(9.5); assert not has_q(dps[1]); rnd(9.6)
    if mode=='dynamic':
        for i in (1,2,3,4): assert has_q(dps[i]), i
        qs={u:[a for a in ins[0].actions] for u,ins in class_actions(dps[1]).items()}
        assert qs[5001][0]==('set_queue',2) and qs[5002][0]==('set_queue',1) and qs[5003][0]==('set_queue',0), qs
        assert c.prio_on
    else:
        assert not has_q(dps[1]) and not c.prio_on
    for _ in range(4): rnd(0.2)
    assert not has_q(dps[1]) and not c.prio_on, 'priority should be off after calm'
    ev=[json.loads(l) for l in open('/tmp/ev_%s.jsonl'%mode)]
    kinds=[e['event'] for e in ev]
    assert 'congestion_start' in kinds and 'congestion_end' in kinds and 'stats' in kinds, kinds
    cs=[e for e in ev if e['event']=='congestion_start'][0]; assert cs['reacted']==(mode=='dynamic'), cs
    # link failure reroutes and keeps class rules on the new port
    if mode=='dynamic':
        # force congestion again so rules have queues, then fail s1-s2 link
        rnd(9.9); rnd(9.9); assert c.prio_on
        d=dps[1]; d_desc=Obj(state=1,config=0,port_no=5)
        c.on_port_status(Obj(msg=Obj(datapath=d,desc=d_desc,reason=0)))
        outs={u:[a for a in ins[0].actions if a[0]=='output'] for u,ins in class_actions(dps[1]).items()}
        assert all(v==[('output',6)] for v in outs.values()), outs
        assert has_q(dps[1])
    print(mode,'OK',len(ev),'events')
print('mock harness passed')
