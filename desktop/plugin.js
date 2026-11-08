import { ROUTES_AREA, SIDEBAR_NAV_AREA, host, useValue } from '@hermes/plugin-sdk'
import { createElement as h, useState, useEffect } from 'react'

let pluginCtx
const ID = 'hermes-reliability'
const styles = {page:{padding:24,maxWidth:1200,margin:'auto'}, row:{display:'flex',gap:12,flexWrap:'wrap',alignItems:'center'}, panel:{padding:18,border:'1px solid var(--border, #444)',borderRadius:12,margin:'16px 0'}, input:{padding:10,background:'transparent',color:'inherit',border:'1px solid var(--border, #555)',borderRadius:6}, button:{padding:'9px 15px',borderRadius:6,cursor:'pointer'}}
function Page() {
  const profile = useValue(host.state.profile)
  const connection = useValue(host.state.connectionId)
  const [data,setData] = useState({campaigns:[],jobs:[]})
  const [source,setSource] = useState('')
  const [runtime,setRuntime] = useState('custom')
  const [scope,setScope] = useState('Explore reliability of realistic workflows, using meaningful controls and code-grounded failure hypotheses.')
  const [execute,setExecute] = useState(false)
  const [mode,setMode] = useState('explore')
  const [benchmark,setBenchmark] = useState('')
  const [baseline,setBaseline] = useState('')
  const [compareCurrent,setCompareCurrent] = useState('')
  const [comparison,setComparison] = useState(null)
  const [memoryRequest,setMemoryRequest] = useState('')
  const [defaults,setDefaults] = useState({})
  const [detail,setDetail] = useState(null)
  const [selected,setSelected] = useState(null)
  const [error,setError] = useState('')
  const [busy,setBusy] = useState(false)
  const owner = () => host.state.profile.get() === profile && host.state.connectionId.get() === connection
  const request = async (path, options) => {
    if (!owner()) throw new Error('Profile changed; reload this view.')
    const value = await pluginCtx.rest(path, {timeoutMs:30000,...options})
    if (!owner()) throw new Error('Profile changed while loading.')
    if (value?.error) throw new Error(String(value.error))
    return value
  }
  useEffect(() => {
    let stopped = false
    let timer
    setDetail(null); setData({campaigns:[],jobs:[]}); setExecute(false)
    setBenchmark(''); setBaseline(''); setComparison(null); setMemoryRequest('')
    async function poll() {
      try {
        const [current,opts] = await Promise.all([request('/campaigns'),request('/defaults')])
        if (!stopped) { setData(current); setDefaults(opts); setError('') }
        if (selected) {
          const next = await request('/campaign/' + selected)
          if (!stopped) setDetail(next)
        }
      } catch (err) { if (!stopped) setError(err.message) }
      if (!stopped) timer = setTimeout(poll,4000)
    }
    poll()
    return () => { stopped = true; clearTimeout(timer) }
  }, [profile,connection,selected])
  async function start(event) {
    event.preventDefault(); setBusy(true)
    try {
      await request('/start', {method:'POST',body:{source,runtime,scope,execute,mode,...(mode==='regression'?{benchmark_id:benchmark,baseline_run_id:baseline}:{})}})
      setData(await request('/campaigns')); setError('')
    } catch(err) { setError(err.message) }
    finally { setBusy(false) }
  }
  const json = (title,value) => h('details',{style:styles.panel},h('summary',null,title),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:500,overflow:'auto'}},JSON.stringify(value,null,2)))
  const runLabel = r => `${r.runtime} · ${String(r.revision||'snapshot').slice(0,10)} · ${r.valid?r.status:'invalid / '+r.status} · ${r.id.slice(0,10)}`
  const action = async (path,body,receive=()=>{}) => {
    try { const result=await request(path,{method:'POST',body}); receive(result); setData(await request('/campaigns')); setError('') }
    catch(e) { setError(e.message) }
  }
  return h('main',{style:styles.page},
    h('p',null,'HERMES / RELIABILITY LAB'),
    h('h1',null,'From source code to evidence'),
    h('p',null,'Real usage datasets → architecture-aware scenarios → three independent reviews → controlled execution → causal findings.'),
    error && h('p',{role:'alert',style:{color:'#e97979'}},error),
    h('form',{onSubmit:start,style:styles.panel},
      h('h2',null,'Connect an agent'),
      h('div',{style:styles.row},
        h('input',{style:{...styles.input,flex:1,minWidth:250},value:source,onChange:e=>setSource(e.target.value),required:true,placeholder:'HTTPS Git repository or local code directory','aria-label':'Agent source'}),
        h('select',{style:styles.input,value:runtime,onChange:e=>setRuntime(e.target.value),'aria-label':'Runtime'},...['custom','hermes','openhands','deerflow'].map(x=>h('option',{key:x,value:x},x)))),
      h('textarea',{style:{...styles.input,width:'100%',marginTop:12},value:scope,onChange:e=>setScope(e.target.value),'aria-label':'Exploration scope'}),
      h('label',null,'Mode ',h('select',{style:styles.input,value:mode,onChange:e=>setMode(e.target.value)},h('option',{value:'explore'},'Explore and refine'),h('option',{value:'regression'},'Frozen benchmark regression'))),
      mode==='regression' && h('div',null,
        h('p',null,'Rerun unchanged scenarios and evaluator. Choose the baseline to compare; no generation calls are made.'),
        h('select',{style:styles.input,value:benchmark,required:true,'aria-label':'Frozen benchmark',onChange:e=>{setBenchmark(e.target.value);setBaseline('')}},h('option',{value:''},'Choose benchmark'),...(data.benchmarks||[]).filter(b=>b.runtime===runtime).map(b=>h('option',{key:b.id,value:b.id},`${b.repository} · ${b.id.slice(0,12)}`))),
        h('select',{style:styles.input,value:baseline,required:true,'aria-label':'Baseline run',onChange:e=>setBaseline(e.target.value)},h('option',{value:''},'Choose baseline'),...(data.runs||[]).filter(r=>r.valid&&r.lane==='trusted'&&r.benchmark_id===benchmark).map(r=>h('option',{key:r.id,value:r.id},runLabel(r))))),
      h('p',null,'Orchestration uses your active Hermes model. Generated scenarios are private to this profile.'),
      h('label',null,h('input',{type:'checkbox',checked:execute,disabled:!defaults.execution_enabled,onChange:e=>setExecute(e.target.checked)}),' Execute generated tests with my configured runtime'),
      h('p',null,defaults.execution_enabled ? 'Execution runs target and generated evaluator code; model usage is billed to your configured account.' : 'To execute, configure a target runtime and enable execution in plugin settings.'),
      h('button',{type:'submit',disabled:busy,style:styles.button},busy?'Launching…':'Start campaign')),
    h('h2',null,'Campaigns'),
    data.jobs.filter(j=>['queued','running','error','interrupted'].includes(j.status)).map(j=>h('div',{key:j.id,style:styles.panel},h('b',null,j.status),h('p',null,j.error||j.id),['queued','running'].includes(j.status)&&h('button',{style:styles.button,onClick:async()=>{try{await request('/stop',{method:'POST',body:{job_id:j.id}})}catch(e){setError(e.message)}}},'Stop at next boundary'))),
    !data.campaigns.length && h('p',null,'Your generated campaigns will appear here. No benchmark experiments are bundled.'),
    data.campaigns.map(c=>h('button',{key:c.id,style:{...styles.button,margin:5},onClick:()=>setSelected(c.id)},`${c.runtime} · ${c.stop_reason||c.phase} · ${c.revision.slice(0,10)}`)),
    h('section',{style:styles.panel},h('h2',null,'Benchmarks and run history'),
      h('button',{style:styles.button,onClick:()=>action('/index-history',{})},'Import earlier runs'),
      json('Frozen benchmarks',data.benchmarks||[]),json('Saved execution attempts',data.runs||[]),
      h('select',{style:styles.input,value:baseline,'aria-label':'Comparison baseline',onChange:e=>setBaseline(e.target.value)},h('option',{value:''},'Choose baseline run'),...(data.runs||[]).filter(r=>r.lane).map(r=>h('option',{key:r.id,value:r.id},runLabel(r)))),
      h('select',{style:styles.input,value:compareCurrent,'aria-label':'Comparison current run',onChange:e=>setCompareCurrent(e.target.value)},h('option',{value:''},'Choose current run'),...(data.runs||[]).filter(r=>r.lane).map(r=>h('option',{key:r.id,value:r.id},runLabel(r)))),
      h('button',{style:styles.button,disabled:!baseline||!compareCurrent,onClick:()=>action('/compare',{baseline_run_id:baseline,current_run_id:compareCurrent},setComparison)},'Compare runs'),
      comparison && json('Comparison and validity',comparison),
      h('button',{style:styles.button,disabled:!baseline,onClick:()=>{const r=(data.runs||[]).find(r=>r.id===baseline);if(r)action('/memory-note',{benchmark_id:r.benchmark_id,baseline_run_id:r.id},()=>setMemoryRequest(`Remember my Reliability Lab baseline. Call reliability_memory with benchmark_id=${r.benchmark_id} and baseline_run_id=${r.id}, then use the native memory tool to save the returned reference.`))}},'Prepare memory note'),
      memoryRequest && h('div',null,h('p',null,'Memory has not been written. Send this request to Hermes; it will report whether its native memory tool saved or staged the note.'),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},memoryRequest))),
    detail && h('section',null,h('h2',null,'Campaign evidence'),
      h('p',null,detail.progress?.phase),
      h('button',{style:styles.button,disabled:detail.progress?.phase!=='terminal',onClick:()=>action('/freeze',{campaign_id:detail.id},r=>{setBenchmark(r.benchmark_id);setBaseline(r.baseline_run_id)})},'Freeze latest valid trusted round'),
      detail.mode==='regression'&&json('Regression comparison',detail.regression),
      json('Dataset provenance',detail.datasets),json('Generated scenarios',detail.tests),json('Reviews, execution and causal findings',detail.rounds),json('Complete campaign record',detail))
  )
}
export default {
  id:ID, name:'Reliability Lab', defaultEnabled:false,
  register(ctx) {
    pluginCtx=ctx
    ctx.registerMany([
      {id:'page',area:ROUTES_AREA,data:{path:'/reliability'},render:()=>h(Page)},
      {id:'nav',area:SIDEBAR_NAV_AREA,data:{path:'/reliability',label:'Reliability Lab',codicon:'beaker'}}
    ])
    ctx.onDispose(()=>{pluginCtx=null})
  }
}
