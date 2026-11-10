'use strict';
function requireWebEnabled(){
  if(module.exports.WEB_CONNECTIONS_PAUSED)throw new Error('All web connections are paused by owner request. Saved data is preserved.');
}
module.exports={WEB_CONNECTIONS_PAUSED:true,requireWebEnabled};
