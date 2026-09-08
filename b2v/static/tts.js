(() => {
  const form=$('tts-form');
  let ident=null,selectedDoc=null,models=[],defaults={},saved=null,connected=false,pending=false,selection=0,audioKey=null,auditionURL=null;
  const numeric=['speaker_id','style_weight','speed','noise','noise_w','pitch_scale','intonation_scale'];
  const notice=text=>$('tts-message').textContent=text;
  function active(){return selectedDoc?.busy==='WAV';}
  function enable(){
    $('tts-fields').disabled=!connected||!models.length||pending||!!selectedDoc?.busy;
    $('tts-production-fields').disabled=!ident||!connected||!models.length||pending||!!selectedDoc?.busy;
    $('tts-generate').disabled=!selectedDoc?.assets?.text;
    $('tts-save').disabled=!ident;
    $('tts-stop').hidden=!active();
    $('tts-stop').disabled=!!selectedDoc?.audio_run?.stop_requested;
  }
  function choices(element,entries,value){
    element.replaceChildren();
    for(const [id,name] of entries){const option=node('option',name);option.value=id;element.append(option);}
    if(entries.some(([id])=>String(id)===String(value)))element.value=value;
  }
  function modelChildren(settings={}){
    const model=models.find(m=>m.id===$('tts-model').value);
    choices($('tts-speaker'),(model?.speakers||[]).map(s=>[s.id,s.name]),settings.speaker_id);
    choices($('tts-style'),(model?.styles||[]).map(s=>[s,s]),settings.style||'Neutral');
  }
  function fill(settings){
    const source={...defaults,...settings};
    choices($('tts-model'),models.map(m=>[m.id,m.name]),source.model_id);modelChildren(source);
    for(const key of numeric.filter(k=>k!=='speaker_id'))if(source[key]!==undefined)form.elements.namedItem(key).value=source[key];
    $('tts-weight-range').value=$('tts-weight').value;$('tts-speed-range').value=$('tts-speed').value;
    if(source.model_id&&!models.some(m=>m.id===source.model_id))notice('保存済みモデルがありません。Modelを選択してください。');
  }
  function payload(){
    const settings={};
    for(const key of ['model_id','style',...numeric]){const value=form.elements.namedItem(key).value;settings[key]=numeric.includes(key)?Number(value):value;}
    return {settings,max_chunk_chars:Number($('tts-max-chars').value)};
  }
  function durationText(seconds){const minutes=Math.ceil(Math.max(0,seconds)/60);return minutes>=60?`${Math.floor(minutes/60)}時間${minutes%60}分`:`${minutes}分`;}
  function render(){
    if(!selectedDoc)return;
    $('tts-production-document').textContent=`本番生成する本：${selectedDoc.name}`;
    const run=selectedDoc.audio_run,result=selectedDoc.audio_result;
    $('tts-generate').textContent=run&&['stopped','failed','interrupted'].includes(run.status)?'本全体のWAV生成を再開':result?'本全体のWAVを再生成':'本全体のWAV生成を開始';
    if(!selectedDoc.final_current&&selectedDoc.assets?.text)$('tts-generate').textContent='修正本文から最終TXTを更新してWAV生成を再開';
    $('tts-progress-panel').hidden=!run;
    if(run){
      const done=run.processed||0,total=run.total||0,generated=done-(run.reused||0);
      $('tts-progress').max=total||1;$('tts-progress').value=done;
      $('tts-progress-percent').textContent=`${total?Math.floor(done/total*100):0}%`;
      $('tts-progress-count').textContent=`${done} / ${total} chunk 完了${run.reused?`（再利用 ${run.reused}）`:''}`;
      $('tts-progress-elapsed').textContent=`今回の音声生成時間 ${durationText(run.tts_seconds||0)}`;
      $('tts-progress-remaining').textContent=run.status==='completed'?'結合完了':run.status==='joining'?'最終WAVを結合中':run.status==='generating'&&generated>0?`残り約 ${durationText((run.tts_seconds||0)/generated*Math.max(0,total-done))}`:run.status==='generating'||run.status==='queued'?'残り時間を計算中…':'停止中';
      const count=`Chunk ${run.processed||0} / ${run.total||0}（${run.total?Math.floor((run.processed||0)/run.total*100):0}%）`;
      const labels={queued:'開始待ち',generating:'WAV生成中',joining:'全chunk完成・WAVを結合中',stopped:'停止しました',interrupted:'中断されています',completed:'WAV生成完了',failed:'WAV生成に失敗しました'};
      notice(`${labels[run.status]||run.status} — ${count}${run.reused?` / 再利用 ${run.reused}`:''}${run.stop_requested?' / 現在のchunk終了後に停止します':''}${run.error?'\n'+run.error:''}`);
    }else notice(selectedDoc.final_current?'設定を確認して「本全体のWAV生成を開始」を押してください。':'先に「7. 最終テキスト」で現在の本文からTXTを生成してください。');
    $('tts-error-panel').hidden=run?.status!=='failed';
    $('tts-error-pages').replaceChildren();
    if(run?.status==='failed'){
      const context=run.error_context, longError=run.long_sentence;
      const numbers=context?.pages||(longError?.page_number?[longError.page_number]:[]);
      const location=numbers.length?`PDF ${numbers.join('・')}ページ`:'ページ特定なし';
      const excerpt=context?.text||longError?.text||'';
      $('tts-error-text').value=`原因：${run.error||'不明なエラー'}\n箇所：${location}${context?.chunk?` / chunk ${context.chunk}`:''}\n本文：${excerpt.replace(/\s+/g,' ').trim()||'失敗時の本文記録がありません。上記は前回のエラーです。'}`;
      for(const number of numbers){const button=node('button',`PDF ${number}ページを校正`,'secondary');button.type='button';button.onclick=()=>window.openReviewPage(number);$('tts-error-pages').append(button);}
    }
    if(!selectedDoc.final_current&&selectedDoc.assets?.text)notice('本文が編集されています。下の再開ボタンで最終TXTを更新し、成功済みchunkを再利用して音声生成を再開します。');
    const long=run?.long_sentence;$('tts-long').hidden=!long;
    if(long){$('tts-long-info').textContent=`${long.characters}文字 / 最大${long.maximum}文字${long.page_number?` / PDF ${long.page_number}ページ`:''}`;$('tts-long-text').textContent=long.text;$('tts-review').hidden=!long.page_number;}
    $('tts-result').hidden=!result;
    const mp3=selectedDoc.mp3_result, runMP3=selectedDoc.mp3_run;
    $('mp3-generate').disabled=!result||!!selectedDoc.busy;
    $('mp3-delete').disabled=!!selectedDoc.busy;
    $('mp3-message').textContent=selectedDoc.busy==='MP3'?'MP3生成中…':runMP3?.error||runMP3?.warning||(mp3?'MP3完成':result?'MP3を生成できます。':'先にWAVを生成してください。');
    $('mp3-result').hidden=!mp3;
    const key=mp3?ident+mp3.filename:null;
    if(key!==audioKey){const audio=$('mp3-audio');audio.pause();if(mp3)audio.src=`/api/documents/${ident}/tts/mp3?v=${mp3.filename}`;else audio.removeAttribute('src');audio.load();audioKey=key;}
    if(mp3){$('mp3-info').textContent=`${`${Math.floor(mp3.duration/3600)}時間${Math.floor(mp3.duration%3600/60)}分${Math.floor(mp3.duration%60)}秒`} / ${(mp3.size_bytes/1024/1024).toFixed(1)} MB / 96 kbps / mono`;$('mp3-download').href=`/api/documents/${ident}/tts/mp3?download=true`;}
    enable();
  }
  function changed(){}
  $('mp3-generate').onclick=async()=>{try{await request(`/api/documents/${ident}/tts/mp3`,{method:'POST'});await refresh();}catch(e){$('mp3-message').textContent=e.message;}};
  $('mp3-delete').onclick=async()=>{try{await request(`/api/documents/${ident}/tts/mp3`,{method:'DELETE'});await refresh();}catch(e){$('mp3-message').textContent=e.message;}};
  async function connect(){
    $('tts-health').textContent='接続確認中…';
    try{const health=await request('/api/tts/health');if(!health.ready)throw new Error(health.message);
      const result=await request('/api/tts/models');const draft=connected&&ident?payload().settings:saved;
      models=result.models;defaults=result.defaults;connected=true;fill(draft);
      $('tts-health').textContent=models.length?'Style-Bert-VITS2: 利用可能':'利用可能なモデルがありません。';
    }catch(e){connected=false;$('tts-health').textContent=e.message;}enable();
  }
  window.ttsUI={
    selectDocument(document){selection++;ident=document.id;selectedDoc=document;saved=document.tts_settings||null;$('tts-max-chars').value=document.tts_max_chars||300;fill(saved);render();},
    refreshDocument(document){if(document.id!==ident)return;selectedDoc=document;render();}
  };
  $('tts-connect').onclick=connect;$('tts-model').onchange=()=>{modelChildren();changed();};
  for(const [range,number] of [['tts-weight-range','tts-weight'],['tts-speed-range','tts-speed']]){
    $(range).oninput=()=>{$(number).value=$(range).value;changed();};$(number).oninput=()=>{$(range).value=$(number).value;changed();};
  }
  form.addEventListener('input',changed);
  $('tts-audition').onclick=async()=>{
    const text=$('tts-audition-text').value;
    if(!text.trim()||Array.from(text).length>150){$('tts-audition-message').textContent='1〜150文字の文章を入力してください。';return;}
    const settings=payload().settings;
    pending=true;enable();$('tts-audition-message').textContent='試聴音声を生成中…';
    try{
      const response=await fetch('/api/tts/audition',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,settings})});
      if(!response.ok){const error=await response.json();throw new Error(typeof error.detail==='string'?error.detail:'試聴音声を生成できませんでした。');}
      const blob=await response.blob(),audio=$('tts-audition-audio');audio.pause();
      if(auditionURL)URL.revokeObjectURL(auditionURL);
      auditionURL=URL.createObjectURL(blob);audio.src=auditionURL;audio.hidden=false;audio.load();
      $('tts-audition-message').textContent=`試聴音声ができました。Speed ${settings.speed} / Intonation ${settings.intonation_scale}。再生して確認してください。`;
    }catch(e){$('tts-audition-message').textContent=e.message;}
    finally{pending=false;enable();}
  };
  $('tts-save').onclick=async()=>{
    if(!form.reportValidity()||!ident)return;const target=ident,serial=selection;
    try{const body=payload();await request(`/api/documents/${target}/tts`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(serial===selection){saved=body.settings;notice('音声設定を保存しました。');}}
    catch(e){if(serial===selection)notice(e.message);}
  };
  form.onsubmit=e=>e.preventDefault();
  $('tts-production-form').onsubmit=async e=>{
    e.preventDefault();if(e.submitter!==$('tts-generate')||!ident||pending||!form.reportValidity()||!$('tts-production-form').reportValidity())return;
    if(dirty){notice('未保存の本文があります。保存し、最終テキストを再生成してください。');return;}
    const target=ident,serial=selection,body=payload();pending=true;enable();
    try{if(!selectedDoc.final_current)await request(`/api/documents/${target}/final`,{method:'POST'});await request(`/api/documents/${target}/tts/wav`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(serial===selection)saved=body.settings;await refresh();}
    catch(error){await refresh();if(serial===selection)notice(error.message);}
    finally{pending=false;enable();}
  };
  $('tts-stop').onclick=async()=>{try{await request(`/api/documents/${ident}/tts/stop`,{method:'POST'});await refresh();}catch(e){notice(e.message);}};
  $('tts-review').onclick=()=>{const number=selectedDoc?.audio_run?.long_sentence?.page_number;if(number)window.openReviewPage(number);};
  connect();
})();
