(()=>{
  'use strict';
  const API='https://czyktvyxebktbocbquuo.supabase.co';
  // Publishable key is intentionally public. RLS protects the data, not this key.
  const KEY='sb_publishable_DW5QQZC4fnsCMVOxWsIVvA_KFFhYOhW';
  const OWNER='fa844f39-5786-498b-aefe-0a6bb497d97e';
  const $=id=>document.getElementById(id);
  let session=null,blobUrl=null,expiryTimer=null,generation=0,syncTimer=null,syncBusy=false,currentRelease=null,currentCode=null;
  let workspaceClient=null;
  const workspaceStatus=(message,error=false)=>{
    const el=$('workspace-status');el.hidden=false;el.textContent=message;el.classList.toggle('error',error);
  };
  async function connectWorkspace(frame,attempt){
    const started=Date.now();
    while(!frame.contentWindow?.__CHANCE_LIGA_INTEGRATED__){
      if(attempt!==generation||Date.now()-started>45000)throw Error('Databázová aplikace se nenačetla.');
      await new Promise(r=>setTimeout(r,150));
    }
    const app=frame.contentWindow;
    workspaceClient=PredictionsWorkspaceBrowser.createWorkspaceClient({
      request:(path,opts)=>{if(attempt!==generation||!session)throw Error('Přihlášení vypršelo.');return request(path,{...opts,token:session.access_token});},
      readRuntime:()=>app.PrivateChanceLiga.captureWorkspace(),
      applyRuntime:(state,locks)=>{if(attempt!==generation||!app.PrivateChanceLiga.restoreWorkspace(state,locks))throw Error('Databázový stav neprošel kontrolou.');},
      onStatus:s=>{if(attempt!==generation)return;
        if(s.type==='saving')workspaceStatus('Ukládám do společné databáze…');
        if(s.type==='saved')workspaceStatus('Uloženo ve společné databázi');
        if(s.type==='error')workspaceStatus(s.error?.status===409?'Změna není uložená: jiné zařízení nebo automat už provedly zápis. Před obnovením vyexportuj neuložené změny.':'Změna není uložená. Zkus Obnovit data; před odchodem případně vyexportuj neuložené změny.',true);
      }
    });
    await workspaceClient.initialize();
    if(attempt!==generation)throw Error('Přihlášení vypršelo.');
    window.PredictionsWorkspaceBridge=Object.freeze({save:(lock)=>void workspaceClient?.save(lock).catch(e=>{
      if(attempt===generation&&e.status!==409)workspaceStatus('Změna není uložená: '+e.message,true);
    })});
    app.__PREDICTIONS_SHARED_READY__=true;
  }
  function syncStatus(message,error=false){$('sync-status').textContent=message;$('sync-status').style.color=error?'#ffb3ae':'#82d6b6';}
  function payloadFrom(html){
    const matches=[...html.matchAll(/<script id="private-release-data" type="application\/json">([\s\S]*?)<\/script>/g)];
    if(matches.length!==1)throw new Error('Nová verze vyžaduje opětovné přihlášení.');
    const payload=JSON.parse(matches[0][1]);
    if(!/^[a-f0-9]{64}$/.test(payload.codeHash||''))throw new Error('Neplatná identita soukromé aplikace.');
    return payload;
  }
  async function syncPrivateData(){
    if(!session||syncBusy)return;
    const attempt=generation;syncBusy=true;
    try{
      if(workspaceClient?.unsaved()){await workspaceClient.retry();if(workspaceClient.unsaved())return;}
      const rows=await request('/rest/v1/predictions_private_manifest?id=eq.current&select=*',{token:session.access_token});
      if(rows.length!==1)throw new Error('Soukromá publikace není dostupná.');
      const m=PredictionsAuthCore.validateManifest(rows[0]);
      if(attempt!==generation||!session)return;
      const frame=$('app-container').querySelector('iframe'),doc=frame?.contentDocument;
      const editing=()=>doc?.activeElement?.matches('input,textarea,select,[contenteditable="true"]')||doc?.querySelector('.add-form.on,dialog[open],.modal-overlay.open');
      if(m.release_id===currentRelease){if(workspaceClient&&!editing())await workspaceClient.refresh();syncStatus('Publikovaná verze načtena');return;}
      syncStatus('Načítám nová soukromá data…');
      const parts=await request('/rest/v1/predictions_private_parts?release_id=eq.'+m.release_id+'&select=release_id,part,payload&order=part.asc&limit=256',{token:session.access_token});
      const html=await PredictionsAuthCore.decodeBundle(m,parts),payload=payloadFrom(html);
      if(attempt!==generation||!session||Date.now()>=session.expiresAt-15000)return;
      if(payload.codeHash!==currentCode)throw new Error('Je dostupný nový kód aplikace. Odhlas se a znovu přihlas.');
      if(!frame?.contentWindow?.__CHANCE_LIGA_INTEGRATED__)throw new Error('Aplikace se ještě načítá.');
      if(editing()||workspaceClient?.unsaved()){
        syncStatus('Nová data čekají na dokončení editace');return;
      }
      frame.contentWindow.PrivateChanceLiga.applyRelease(payload);
      if(workspaceClient)await workspaceClient.refresh();
      currentRelease=m.release_id;syncStatus('Soukromá data aktualizována');
    }catch(error){if(attempt===generation&&session)syncStatus('Synchronizace: '+error.message,true);}
    finally{if(attempt===generation)syncBusy=false;}
  }
  function status(message,error=false){$('status').textContent=message;$('status').classList.toggle('error',error);}
  async function request(path,{method='GET',body,token}={}){
    const response=await fetch(API+path,{method,cache:'no-store',credentials:'omit',referrerPolicy:'no-referrer',
      headers:{apikey:KEY,...(token?{Authorization:'Bearer '+token}:{}),...(body?{'Content-Type':'application/json'}:{})},
      ...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(30000)});
    if(!response.ok){const error=new Error(response.status===400||response.status===401?'Přihlášení nebylo přijato. Zkontrolujte e-mail a heslo.':response.status===403?'Tento účet nemá přístup k Predictions.':'Server není dostupný nebo odmítl požadavek ('+response.status+').');error.status=response.status;throw error;}
    return response.status===204?null:response.json();
  }
  function closeView(message){
    generation++;session=null;clearTimeout(expiryTimer);expiryTimer=null;clearInterval(syncTimer);syncTimer=null;currentRelease=null;currentCode=null;syncBusy=false;
    $('app-container').replaceChildren();if(blobUrl)URL.revokeObjectURL(blobUrl);blobUrl=null;
    workspaceClient=null;delete window.PredictionsWorkspaceBridge;$('workspace-status').hidden=true;
    $('workspace').hidden=true;$('login').hidden=false;document.body.classList.remove('open');
    $('submit').disabled=false;$('password').value='';status(message);$('password').focus();
  }
  async function signOut(){
    if(workspaceClient?.unsaved()){workspaceStatus('Odhlášení pozastaveno: změny ještě nejsou uložené. Zkus Obnovit data nebo vytvoř export.',true);return;}
    const token=session?.access_token;closeView('Jste odhlášen/a. Uložené predikce zůstaly zachované.');
    // Local scope avoids logging out the separate Christmas application.
    if(token)try{await request('/auth/v1/logout?scope=local',{method:'POST',token});}catch{status('Zobrazení je uzavřené. Serverové odhlášení se nepodařilo; relace vyprší automaticky.',true);}
  }
  $('logout').addEventListener('click',signOut);
  $('refresh-data').addEventListener('click',()=>void syncPrivateData());
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault();const attempt=++generation;$('submit').disabled=true;status('Ověřuji přihlášení…');
    let credentials={email:$('email').value.trim(),password:$('password').value};$('password').value='';
    try{
      const authenticated=await request('/auth/v1/token?grant_type=password',{method:'POST',body:credentials});credentials=null;
      if(attempt!==generation)return;
      if(!authenticated.access_token||!Number.isFinite(authenticated.expires_in)||authenticated.expires_in<=0)throw new Error('Neúplná přihlašovací relace.');
      const user=await request('/auth/v1/user',{token:authenticated.access_token});
      if(user.id!==OWNER){await request('/auth/v1/logout?scope=local',{method:'POST',token:authenticated.access_token});throw new Error('Tento účet nemá přístup k Predictions.');}
      if(attempt!==generation)return;
      session={access_token:authenticated.access_token,expiresAt:Date.now()+authenticated.expires_in*1000};
      expiryTimer=setTimeout(()=>closeView('Přihlášení vypršelo. Přihlaste se znovu.'),Math.max(0,authenticated.expires_in*1000-15000));
      status('Přihlášeno. Načítám soukromou aplikaci…');
      const manifests=await request('/rest/v1/predictions_private_manifest?id=eq.current&select=*',{token:session.access_token});
      if(manifests.length!==1)throw new Error('Soukromá aplikace není publikovaná nebo nemáte přístup.');
      const m=PredictionsAuthCore.validateManifest(manifests[0]);
      const parts=await request('/rest/v1/predictions_private_parts?release_id=eq.'+m.release_id+'&select=release_id,part,payload&order=part.asc&limit=256',{token:session.access_token});
      const html=await PredictionsAuthCore.decodeBundle(m,parts);
      if(attempt!==generation||!session||Date.now()>=session.expiresAt-15000)return;
      currentRelease=m.release_id;currentCode=payloadFrom(html).codeHash;
      blobUrl=URL.createObjectURL(new Blob([html],{type:'text/html;charset=utf-8'}));
      const frame=document.createElement('iframe');frame.title='Soukromá aplikace Predictions';frame.referrerPolicy='no-referrer';
      // Same-origin access preserves the existing local prediction journal.
      frame.src=blobUrl;$('app-container').replaceChildren(frame);
      if(html.includes('window.__PREDICTIONS_SHARED_REQUIRED__=true;'))await connectWorkspace(frame,attempt);
      $('login').hidden=true;$('workspace').hidden=false;document.body.classList.add('open');
      syncStatus('Publikovaná verze načtena');syncTimer=setInterval(()=>void syncPrivateData(),5*60*1000);
    }catch(error){
      credentials=null;if(attempt!==generation)return;
      const token=session?.access_token;closeView(error.message||'Přihlášení se nezdařilo.');$('status').classList.add('error');
      if(token)try{await request('/auth/v1/logout?scope=local',{method:'POST',token});}catch{}
    }finally{if(attempt===generation)$('submit').disabled=false;credentials=null;}
  });
  window.addEventListener('pageshow',event=>{if(event.persisted)closeView('Přihlaste se znovu.');});
  window.addEventListener('beforeunload',event=>{if(workspaceClient?.unsaved()){event.preventDefault();event.returnValue='';}});
  document.addEventListener('visibilitychange',()=>{if(session&&Date.now()>=session.expiresAt-15000)closeView('Přihlášení vypršelo. Přihlaste se znovu.');else if(session&&!document.hidden)void syncPrivateData();});
})();
