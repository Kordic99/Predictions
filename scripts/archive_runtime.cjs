'use strict';
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require('playwright');

// Execute the actual application model, not a divergent reimplementation.
async function openApp(root,{browserOptions={},storage=null,runner=true}={}){
  const server=http.createServer((req,res)=>{
    let file;
    try{file=path.resolve(root,'.'+decodeURIComponent(new URL(req.url,'http://localhost').pathname));}catch{res.writeHead(400).end();return;}
    if(file===root)file=path.join(root,'index.html');
    if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404).end();return;}
    const type={'.html':'text/html','.js':'text/javascript','.json':'application/json'}[path.extname(file)]||'application/octet-stream';
    res.writeHead(200,{'Content-Type':type+'; charset=utf-8','Cache-Control':'no-store'});
    fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const url='http://127.0.0.1:'+server.address().port;
  let browser;
  try{
    browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{}),...browserOptions});
    const page=await browser.newPage();
    const errors=[];
    page.on('pageerror',error=>errors.push(String(error)));
    await page.route('**/*',route=>route.request().url().startsWith(url+'/')?route.continue():route.abort());
    await page.addInitScript(({runner,storage})=>{
      window.__PREDICTION_ARCHIVE_RUNNER__=runner;
      window.alert=()=>{};window.confirm=()=>true;
      if(storage)for(const [key,value]of Object.entries(storage))localStorage.setItem(key,value);
    },{runner,storage});
    await page.goto(url+'/',{waitUntil:'load'});
    if(errors.length)throw new Error(errors.join('\n'));
    return {page,errors,url,close:async()=>{await browser.close();await new Promise(resolve=>server.close(resolve));}};
  }catch(error){if(browser)await browser.close();server.close();throw error;}
}
module.exports={openApp};
