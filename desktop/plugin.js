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
      await request('/start', {method:'POST',body:{source,runtime,scope,execute}})
      setData(await request('/campaigns')); setError('')
    } catch(err) { setError(err.message) }
    finally { setBusy(false) }
  }
  const json = (title,value) => h('details',{style:styles.panel},h('summary',null,title),h('pre',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere',maxHeight:500,overflow:'auto'}},JSON.stringify(value,null,2)))
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
      h('p',null,'Orchestration uses your active Hermes model. Generated scenarios are private to this profile.'),
      h('label',null,h('input',{type:'checkbox',checked:execute,disabled:!defaults.execution_enabled,onChange:e=>setExecute(e.target.checked)}),' Execute generated tests with my configured runtime'),
      h('p',null,defaults.execution_enabled ? 'Execution runs target and generated evaluator code; model usage is billed to your configured account.' : 'To execute, configure a target runtime and enable execution in plugin settings.'),
      h('button',{type:'submit',disabled:busy,style:styles.button},busy?'Launching…':'Start campaign')),
    h('h2',null,'Campaigns'),
    data.jobs.filter(j=>['queued','running','error','interrupted'].includes(j.status)).map(j=>h('div',{key:j.id,style:styles.panel},h('b',null,j.status),h('p',null,j.error||j.id),['queued','running'].includes(j.status)&&h('button',{style:styles.button,onClick:async()=>{try{await request('/stop',{method:'POST',body:{job_id:j.id}})}catch(e){setError(e.message)}}},'Stop at next boundary'))),
    !data.campaigns.length && h('p',null,'Your generated campaigns will appear here. No benchmark experiments are bundled.'),
    data.campaigns.map(c=>h('button',{key:c.id,style:{...styles.button,margin:5},onClick:()=>setSelected(c.id)},`${c.runtime} · ${c.stop_reason||c.phase} · ${c.revision.slice(0,10)}`)),
    detail && h('section',null,h('h2',null,'Campaign evidence'),
      h('p',null,detail.progress?.phase),
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
