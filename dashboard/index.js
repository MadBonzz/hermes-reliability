(function () {
  'use strict';
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const BASE = '/api/plugins/hermes-reliability';
  const workbenchURL = new URL('workbench/index.html', document.currentScript.src).href;
  // The existing workbench stays isolated from Hermes's CSS. All data calls go
  // through the host's documented fetchJSON so profile/auth headers survive.
  function ReliabilityPage() {
    const ref = SDK.React.useRef(null);
    SDK.React.useEffect(() => {
      const handle = async (event) => {
        if (event.origin !== location.origin || event.source !== ref.current?.contentWindow || event.data?.type !== 'reliability.request') return;
        const {id, path, body} = event.data;
        if (!/^\/(defaults|campaigns|campaign\/[a-f0-9]+|start|stop)$/.test(path)) return;
        try {
          const value = await SDK.fetchJSON(BASE + path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
          event.source.postMessage({type:'reliability.response', id, value}, location.origin);
        } catch (error) {
          event.source.postMessage({type:'reliability.response', id, error:String(error.message || error)}, location.origin);
        }
      };
      window.addEventListener('message', handle);
      return () => window.removeEventListener('message', handle);
    }, []);
    return SDK.React.createElement('iframe', {ref, src:workbenchURL, title:'Reliability Lab', style:{width:'100%', height:'calc(100vh - 130px)', border:0, borderRadius:12}});
  }
  window.__HERMES_PLUGINS__.register('hermes-reliability', ReliabilityPage);
})();
