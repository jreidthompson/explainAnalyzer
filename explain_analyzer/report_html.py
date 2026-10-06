"""Self-contained HTML report: inline CSS/JS, no external resources.

All plan-derived strings are inserted with textContent (never innerHTML) and the
embedded JSON is escaped for use inside a <script> element.
"""
from __future__ import annotations

import json
from typing import Any

from .context import describe, join_on, join_sides
from .model import Finding, Node, Plan, SEVERITY_NAMES
from .report_text import summary_lines


def _scalar_props(n: Node) -> dict[str, Any]:
    out = {}
    for k, v in n.props.items():
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float)) for x in v):
            out[k] = ", ".join(str(x) for x in v)
    return out


def build_data(plan: Plan, findings: list[Finding]) -> dict[str, Any]:
    nodes = []
    for n in plan.nodes:
        m = n.m
        nodes.append({
            "id": n.id, "parent": n.parent.id if n.parent else None,
            "label": n.label(), "rel": n.relationship, "sub": n.subplan_name,
            "never": m["never"], "excl_ms": round(m["excl_ms"], 3),
            "incl_ms": None if m["incl_ms"] is None else round(m["incl_ms"], 3),
            "excl_pct": round(m["excl_pct"], 1), "loops": m["loops"],
            "est": m["est_rows"], "act": m["act_rows"], "mis": round(m["mis"], 1),
            "mis_dir": m["mis_dir"], "mis_ignored": m["mis_ignored"],
            "cost_pct": round(m["cost_pct"], 1),
            "read": m["buf_excl"].get("Shared Read Blocks", 0),
            "hit": m["buf_excl"].get("Shared Hit Blocks", 0),
            "ctx": describe(n), "on": join_on(n, 400), "sides": join_sides(n, 300),
            "props": _scalar_props(n),
        })
    return {
        "analyzed": plan.m["analyzed"],
        "summary": summary_lines(plan),
        "nodes": nodes,
        "findings": [{"rule": f.rule, "sev": f.severity, "sevName": SEVERITY_NAMES[f.severity],
                      "title": f.title, "detail": f.detail, "suggestion": f.suggestion, "ctx": f.context,
                      "node": f.node_id, "impact": round(f.impact_ms, 3)} for f in findings],
    }


def render(plan: Plan, findings: list[Finding], title: str = "EXPLAIN analysis") -> str:
    data = json.dumps(build_data(plan, findings)).replace("<", "\\u003c").replace(">", "\\u003e") \
        .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return _TEMPLATE.replace("__DATA__", data).replace("__TITLE__", "EXPLAIN analysis")


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#1d2330;--mut:#6b7385;--card:#f5f6f9;--bd:#d9dce4;--c:#c4281c;--w:#b36b00;--i:#1b6ca8;--hl:#fff3b0}
@media (prefers-color-scheme:dark){:root{--bg:#14171f;--fg:#e4e7ee;--mut:#9aa2b5;--card:#1d212c;--bd:#323848;--c:#ff6b5e;--w:#f0b04a;--i:#62b0ee;--hl:#4a4310}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;padding:16px;max-width:1300px;margin:auto}
h1{font-size:20px;margin:0 0 8px}h2{font-size:16px;margin:24px 0 8px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}
.sum div{font-family:ui-monospace,monospace;font-size:13px}
.f{border-left:4px solid var(--i);margin:6px 0;cursor:pointer}.f.s3{border-color:var(--c)}.f.s2{border-color:var(--w)}
.f b.s3{color:var(--c)}.f b.s2{color:var(--w)}.f b.s1{color:var(--i)}
.f .d{color:var(--mut);margin-top:2px}.ctx{margin-top:4px;padding-left:8px;border-left:2px solid var(--bd);font-family:ui-monospace,monospace;font-size:12px;color:var(--mut);overflow-wrap:anywhere}.f .sg{margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border-bottom:1px solid var(--bd);padding:4px 6px;text-align:right;white-space:nowrap}
th{cursor:pointer;position:sticky;top:0;background:var(--card);user-select:none}
td.l,th.l{text-align:left;white-space:normal}tr.sel td{background:var(--hl)}tr.never td{color:var(--mut)}
.bar{display:inline-block;height:8px;background:var(--c);border-radius:2px;vertical-align:middle;margin-right:4px}
.tree{font-family:ui-monospace,monospace;font-size:13px}.tree div.n{padding:1px 4px;border-radius:4px;cursor:pointer;white-space:nowrap}
.tree div.n:hover{background:var(--card)}.tree .sel{background:var(--hl)}
.t{display:inline-block;width:14px;color:var(--mut)}.mut{color:var(--mut)}
.on{color:var(--i)}.tag3{color:var(--c)}.tag2{color:var(--w)}.tag1{color:var(--i)}
#props{white-space:pre-wrap;font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere}
.wrap{overflow-x:auto}
</style></head><body>
<h1>__TITLE__</h1>
<div class="card sum" id="sum"></div>
<h2>Findings</h2><div id="finds"></div>
<h2>Plan tree <span class="mut">(click to select; click arrow to collapse)</span></h2>
<div class="card tree" id="tree"></div>
<h2>Selected node</h2><div class="card" id="props">Click a node or finding.</div>
<h2>All nodes <span class="mut">(click a header to sort)</span></h2>
<div class="wrap card"><table id="tbl"></table></div>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D=JSON.parse(document.getElementById('data').textContent);
const byId=new Map(D.nodes.map(n=>[n.id,n]));
const kids=new Map();D.nodes.forEach(n=>{if(n.parent!=null){(kids.get(n.parent)||kids.set(n.parent,[]).get(n.parent)).push(n)}});
const sevOf=new Map();D.findings.forEach(f=>{if(f.node!=null)sevOf.set(f.node,Math.max(sevOf.get(f.node)||0,f.sev))});
const $=id=>document.getElementById(id);
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
const fmt=(x,d=0)=>x==null?'':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
let selected=null;
function select(id){
  selected=id;
  document.querySelectorAll('.sel').forEach(e=>e.classList.remove('sel'));
  document.querySelectorAll('[data-id="'+id+'"]').forEach(e=>e.classList.add('sel'));
  const n=byId.get(id);const p=$('props');
  p.textContent='';
  if(!n)return;
  let s='#'+n.id+' '+n.label+'\n'+n.ctx.map(x=>'  '+x).join('\n')+'\n\n';
  for(const [k,v] of Object.entries(n.props))s+='  '+k+': '+v+'\n';
  for(const f of D.findings)if(f.node===id)s+='\n['+f.sevName+'] '+f.title+'\n  '+f.detail+'\n  -> '+f.suggestion+'\n';
  p.textContent=s;
  const row=document.querySelector('#tbl tr[data-id="'+id+'"]');if(row)row.scrollIntoView({block:'nearest'});
}
D.summary.forEach(s=>$('sum').appendChild(el('div',null,s)));
if(!D.findings.length)$('finds').appendChild(el('div','card','No problems detected.'));
D.findings.forEach(f=>{
  const d=el('div','card f s'+f.sev);
  const h=el('div');h.appendChild(el('b','s'+f.sev,f.sevName.toUpperCase()+' '));
  h.appendChild(document.createTextNode(f.title+(f.node?'  (node #'+f.node+')':'')));
  d.appendChild(h);
  if(f.detail)d.appendChild(el('div','d',f.detail));
  if(f.ctx&&f.ctx.length){const c=el('div','ctx');f.ctx.forEach(x=>c.appendChild(el('div',null,x)));d.appendChild(c)}
  if(f.suggestion)d.appendChild(el('div','sg','\u2192 '+f.suggestion));
  if(f.node!=null)d.onclick=()=>select(f.node);
  $('finds').appendChild(d);
});
function drawTree(n,depth,parent){
  const row=el('div','n');row.dataset.id=n.id;row.style.paddingLeft=(depth*18)+'px';
  const k=kids.get(n.id)||[];
  const tog=el('span','t',k.length?'\u25BE':'');
  row.appendChild(tog);
  const sv=sevOf.get(n.id)||0;
  if(sv)row.appendChild(el('span','tag'+sv,'\u25CF '));
  row.appendChild(document.createTextNode('#'+n.id+' '+n.label+'  '));
  let st;
  if(n.never)st='never executed';
  else if(D.analyzed)st=fmt(n.excl_ms,2)+' ms ('+fmt(n.excl_pct)+'%)  rows '+fmt(n.est)+'\u2192'+fmt(n.act)+(n.mis>=10&&!n.mis_ignored?' \u00d7'+fmt(n.mis)+' '+n.mis_dir:'')+'  loops '+fmt(n.loops);
  else st='rows '+fmt(n.est)+'  cost '+fmt(n.cost_pct)+'%';
  row.appendChild(el('span','mut',st));
  if(n.on){const o=el('span','on','   on '+n.on);row.appendChild(o)}
  row.onclick=e=>{if(e.target===tog&&k.length){const box=row.nextSibling;const hide=box.style.display!=='none';box.style.display=hide?'none':'';tog.textContent=hide?'\u25B8':'\u25BE'}else select(n.id)};
  parent.appendChild(row);
  const box=el('div');parent.appendChild(box);
  if(n.sub&&depth>=0){}
  k.forEach(c=>{if(c.sub){const s=el('div','mut',c.sub);s.style.paddingLeft=((depth+1)*18+14)+'px';box.appendChild(s)}drawTree(c,depth+1,box)});
}
drawTree(D.nodes[0],0,$('tree'));
const cols=[['id','#',0],['label','Node',1],['excl_ms','Excl ms',0],['excl_pct','Excl %',0],['incl_ms','Incl ms',0],['loops','Loops',0],['est','Est rows',0],['act','Act rows',0],['mis','Misest \u00d7',0],['read','Shared read',0],['hit','Shared hit',0],['on','Join / lookup on',1],['sides','Tables joined',1]];
let sortKey='excl_ms',sortDir=-1;
function drawTable(){
  const t=$('tbl');t.textContent='';
  const hr=el('tr');
  cols.forEach(([k,name,left])=>{const th=el('th',left?'l':'',name+(k===sortKey?(sortDir<0?' \u25BE':' \u25B4'):''));th.onclick=()=>{if(sortKey===k)sortDir=-sortDir;else{sortKey=k;sortDir=left?1:-1}drawTable()};hr.appendChild(th)});
  t.appendChild(hr);
  const rows=D.nodes.slice().sort((a,b)=>{const x=a[sortKey],y=b[sortKey];return (x==null?-1:x)>(y==null?-1:y)?sortDir:(x==y?0:-sortDir)});
  rows.forEach(n=>{
    const tr=el('tr',n.never?'never':'');tr.dataset.id=n.id;tr.onclick=()=>select(n.id);
    cols.forEach(([k,,left])=>{
      const td=el('td',left?'l':'');
      if(k==='excl_pct'){const b=el('span','bar');b.style.width=Math.min(60,n.excl_pct*0.6)+'px';td.appendChild(b);td.appendChild(document.createTextNode(fmt(n.excl_pct,1)))}
      else if(k==='mis')td.textContent=n.mis>1&&!n.mis_ignored?fmt(n.mis)+' '+(n.mis_dir||''):'';
      else if(k==='on'||k==='sides')td.textContent=n[k]||'';
      else td.textContent=(k==='label'||k==='id')?(k==='id'?n.id:n.label):fmt(n[k],k==='excl_ms'||k==='incl_ms'?2:0);
      tr.appendChild(td)});
    t.appendChild(tr)});
  if(selected!=null)select(selected);
}
drawTable();
</script></body></html>
"""
