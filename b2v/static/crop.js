// Display percentages only. Python render_page/crop_image performs the actual crop.
(() => {
  const $ = id => document.getElementById(id);
  const edges = ['top','bottom','left','right'];
  const inputs = Object.fromEntries(edges.map(k=>[k,form.elements.namedItem(k)]));
  let books=[], parent=null, revision=0, loadRevision=0, confirmed=false, ready=false;
  const tokens=new Map();
  const values=()=>Object.fromEntries(edges.map(k=>[k,Number(inputs[k].value)]));
  function valid(){return edges.every(k=>inputs[k].value!=='' && inputs[k].checkValidity() && Number.isFinite(Number(inputs[k].value)));}
  function invalidate(){confirmed=false;$('start').disabled=true;$('crop-confirmation').textContent='cropは未確定です。「このcropを使用」で確定してください。';}
  function draw(){
    const v=values();if(!valid()){$('crop-confirmation').textContent='各辺は0〜30%の数値で指定してください。';$('crop-confirm').disabled=true;return;}
    for(const k of edges)$('crop-rectangle').style[k]=v[k]+'%';
    $('crop-confirm').disabled=!ready;
  }
  for(const k of edges)inputs[k].addEventListener('input',()=>{invalidate();draw();});
  async function clearTokens(){for(const token of tokens.values())request('/api/previews/'+token,{method:'DELETE'}).catch(()=>{});tokens.clear();}
  async function load(page){
    const book=Number($('preview-book').value), current=books[book];if(!current)return;
    const generation=revision, serial=++loadRevision;
    ready=false;$('crop-confirm').disabled=true;$('crop-stage').hidden=true;$('crop-message').textContent='PDFプレビューを読み込み中…（OCRは実行しません）';
    try{
      let token=tokens.get(book);
      if(!parent && !token){
        const data=new FormData();data.append('file',current);
        const uploaded=await request('/api/previews',{method:'POST',body:data});
        if(generation!==revision || serial!==loadRevision){await request('/api/previews/'+uploaded.id,{method:'DELETE'});return;}
        token=uploaded.id;tokens.set(book,token);current.pages=uploaded.pages;
      }
      page=Math.max(1,Math.min(Number(page)||1,current.pages));
      const url=parent ? `/api/documents/${parent}/preview/${page}` : `/api/previews/${token}/pages/${page}`;
      const result=await request(url);
      if(generation!==revision || serial!==loadRevision)return;
      const image=$('crop-image');image.src=result.image;await image.decode();
      if(generation!==revision || serial!==loadRevision)return;
      $('crop-stage').style.setProperty('--page-aspect',result.width/result.height);
      current.pages=result.pages;$('preview-page').value=result.page;$('preview-page').max=result.pages;$('preview-count').textContent=`/ ${result.pages}`;
      $('crop-stage').hidden=false;ready=true;draw();$('crop-message').textContent=`${current.name} · PDF ${result.page}ページ（${result.page%2 ? '奇数' : '偶数'}）`;
    }catch(e){if(serial===loadRevision){ready=false;invalidate();$('crop-message').textContent=e.message;}}
  }
  function choose(list){
    revision++;loadRevision++;books=list;ready=false;invalidate();$('crop-stage').hidden=true;$('crop-confirm').disabled=true;
    $('preview-book').replaceChildren();list.forEach((book,i)=>{const option=node('option',book.name);option.value=i;$('preview-book').append(option);});
    $('preview-book').disabled=!list.length;$('crop-message').textContent='PDFを選択するとプレビューを表示します。';
    if(list.length)load(1);
  }
  window.cropUI={
    get parent(){return parent;},
    confirmed:()=>confirmed && valid(),
    attach(ident){parent=ident;fileInput.required=false;clearTokens();},
    selectFiles(files,error){parent=null;fileInput.required=true;clearTokens();choose(error ? [] : files);},
    restore(job){
      parent=job.id;fileInput.value='';fileInput.setCustomValidity('');fileInput.required=false;clearTokens();
      for(const [key,value] of Object.entries(job.ocr_settings || {})){const input=form.elements.namedItem(key);if(input)input.value=value;}
      form.elements.namedItem('direction').value=job.direction;
      $('selection').textContent='保存済みPDFから再処理：'+job.books.map(b=>b.name).join('、');
      choose(job.books.map(b=>({...b}))); action(()=>window.selectDocument?.(job.id,false));$('crop-editor').scrollIntoView({behavior:'smooth',block:'start'});
    }
  };
  $('preview-book').onchange=()=>load(1);
  $('preview-go').onclick=()=>{if($('preview-page').reportValidity())load($('preview-page').value);};
  $('preview-page').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();$('preview-go').click();}});
  $('preview-prev').onclick=()=>load(Number($('preview-page').value)-1);
  $('preview-next').onclick=()=>load(Number($('preview-page').value)+1);
  document.querySelectorAll('[data-jump]').forEach(button=>button.onclick=()=>{const count=books[Number($('preview-book').value)]?.pages;if(count)load(button.dataset.jump==='front' ? Math.min(5,count) : button.dataset.jump==='middle' ? Math.ceil(count/2) : Math.max(1,count-3));});
  $('crop-confirm').onclick=()=>{if(!ready || !valid())return;confirmed=true;$('start').disabled=false;$('crop-confirmation').textContent='使用するcrop：'+edges.map((k,i)=>['上','下','左','右'][i]+' '+inputs[k].value+'%').join(' / ');};
  document.querySelectorAll('[data-edge]').forEach(handle=>{
    let dragging=false;
    handle.onpointerdown=e=>{e.preventDefault();dragging=true;handle.setPointerCapture(e.pointerId);};
    handle.onpointermove=e=>{
      if(!dragging)return;const rect=$('crop-stage').getBoundingClientRect(), edge=handle.dataset.edge;
      const fraction=edge==='top' ? (e.clientY-rect.top)/rect.height : edge==='bottom' ? (rect.bottom-e.clientY)/rect.height : edge==='left' ? (e.clientX-rect.left)/rect.width : (rect.right-e.clientX)/rect.width;
      inputs[edge].value=(Math.max(0,Math.min(30,fraction*100))).toFixed(1);invalidate();draw();
    };
    handle.onpointerup=handle.onpointercancel=()=>{dragging=false;};
  });
  draw();
})();
