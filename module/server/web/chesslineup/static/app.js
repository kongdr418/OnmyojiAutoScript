'use strict';
(() => {
  const API = '/tool/chesslineup/api';
  const ASSET_VERSION = Date.now().toString(36);
  const freshImage = url => `${url}${url.includes('?')?'&':'?'}v=${ASSET_VERSION}`;
  const PROTECT_IMAGE = freshImage('/tool/chesslineup/static/cards/special_hakuzosu_protect.webp');
  const $ = id => document.getElementById(id);
  const clone = value => JSON.parse(JSON.stringify(value));
  const emptySlot = () => ({shikigami:null,weight:1,equipment:[]});
  const newLineup = () => ({key:`lineup_${Date.now().toString(36)}`,display_name:'自定义阵容',slots:Array.from({length:12},emptySlot),primary_bond:'',secondary_bond:'',hakuzosu_protect_position:null,arakawa_goldfish_position:null});
  const state = {catalog:null,index:{},draft:newLineup(),origin:null,kind:'shikigami',cost:0,selected:null,selectedBond:null,presets:[],dirty:true,drag:null};
  let toastTimer;
  function toast(message,error=false) {
    const node=$('toast');node.textContent=message;node.className=error?'show error':'show';
    clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.className='',5000);
  }
  function markDirty(dirty=true) {
    state.dirty=dirty;$('dirty').textContent=dirty?'未保存':'已保存';
    try {localStorage.setItem('chesslineup-draft-v1',JSON.stringify(state.draft));localStorage.setItem('chesslineup-origin-v1',JSON.stringify(state.origin));} catch (_) {}
  }
  async function request(path,options={}) {
    const response=await fetch(API+path,{headers:{'Content-Type':'application/json'},...options});
    const data=await response.json();
    if(!response.ok) {
      const detail=Array.isArray(data.detail)?data.detail.map(e=>e.msg).join('；'):data.detail;
      throw new Error(detail||'请求失败');
    }
    return data;
  }
  function option(value,label) {const e=document.createElement('option');e.value=value;e.textContent=label;return e;}
  function entry(kind,key) {return state.index[kind]?.[key];}
  function bondCounts(draft,catalog=state.index) {
    const counts={};
    for(const slot of draft.slots) {
      if(slot.shikigami)for(const bond of catalog.shikigami[slot.shikigami].bonds)counts[bond]=(counts[bond]||0)+1;
      for(const gear of slot.equipment)if(gear.kind==='emblem'){
        const bond=catalog.emblem[gear.key].name.replace(/纹章$/,'');counts[bond]=(counts[bond]||0)+1;
      }
    }
    return counts;
  }
  function normalizeSpecials(draft,catalog=state.index) {
    const counts=bondCounts(draft,catalog);
    if((counts['荒川']||0)>=2) {
      if(!draft.arakawa_goldfish_position||draft.slots[draft.arakawa_goldfish_position-1]?.shikigami) {
        const empty= draft.slots.slice().reverse().findIndex(slot=>!slot.shikigami);
        draft.arakawa_goldfish_position=empty<0?null:12-empty;
      }
    }
    else draft.arakawa_goldfish_position=null;
    if(!draft.slots.some(slot=>slot.shikigami==='yume_san_byakuzou'))draft.hakuzosu_protect_position=null;
    if(draft.secondary_bond&&!counts[draft.secondary_bond])draft.secondary_bond='';
    return draft;
  }
  function updateSpecialTargets(draft,protector) {
    const position=draft.slots.findIndex(slot=>slot.shikigami===protector);
    draft.hakuzosu_protect_position=protector&&position>=0&&draft.slots.some(s=>s.shikigami==='yume_san_byakuzou')?position+1:null;
  }
  // Drag sources describe identities/indices; the library is never mutated.
  function applyDrop(draft,payload,target,catalog=state.index) {
    if(!draft||draft.slots.length!==12)throw new Error('阵容数据无效');
    const result=clone(draft);
    let protector=result.hakuzosu_protect_position?result.slots[result.hakuzosu_protect_position-1]?.shikigami:null;
    const fishIndex=result.arakawa_goldfish_position?result.arakawa_goldfish_position-1:null;
    if(target!==null&&(!Number.isInteger(target)||target<0||target>11))throw new Error('槽位无效');
    if(payload.kind==='goldfish') {
      if((bondCounts(result,catalog)['荒川']||0)<2)throw new Error('荒川羁绊达到 2 才会出现金鱼');
      if(target!==null&&result.slots[target].shikigami)throw new Error('请将金鱼拖到空槽位');
      result.arakawa_goldfish_position=target===null?12:target+1;
    } else if(payload.kind==='protect') {
      if(!result.slots.some(slot=>slot.shikigami==='yume_san_byakuzou'))throw new Error('上阵梦山白藏主后才可使用守护之印');
      if(target!==null&&!result.slots[target].shikigami)throw new Error('守护之印必须拖到式神身上');
      protector=target===null?null:result.slots[target].shikigami;
    } else if(payload.kind==='unit') {
      const from=payload.from;
      if(!Number.isInteger(from)||!result.slots[from]?.shikigami)throw new Error('式神来源无效');
      if(target===null)result.slots[from]=emptySlot();
      else {
        if(!Number.isInteger(target)||target<0||target>11)throw new Error('槽位无效');
        if(target===fishIndex)throw new Error('该位置已有金鱼，请先移动金鱼');
        [result.slots[from],result.slots[target]]=[result.slots[target],result.slots[from]];
      }
    } else if(payload.kind==='shikigami') {
      if(target===null)return result;
      if(target===fishIndex)throw new Error('该位置已有金鱼，请先移动金鱼');
      if(!catalog.shikigami[payload.key])throw new Error('未知式神');
      if(!Number.isInteger(target)||target<0||target>11)throw new Error('槽位无效');
      const previous=result.slots.findIndex(slot=>slot.shikigami===payload.key);
      if(previous>=0)[result.slots[previous],result.slots[target]]=[result.slots[target],result.slots[previous]];
      else {
        if(!result.slots[target].shikigami&&result.slots.filter(s=>s.shikigami).length>=10)throw new Error('阵容人口上限为 10');
        result.slots[target]={shikigami:payload.key,weight:1,equipment:[]};
      }
    } else {
      let gear;
      if(payload.kind==='equipped') {
        if(!Number.isInteger(payload.from)||!Number.isInteger(payload.equipment))throw new Error('装备来源无效');
        gear=result.slots[payload.from]?.equipment[payload.equipment];
        if(!gear)throw new Error('装备不存在');
        if(target===payload.from)return result;
      } else if(payload.kind==='soul'||payload.kind==='emblem')gear={kind:payload.kind,key:payload.key};
      else throw new Error('请从素材库或棋盘拖取卡牌');
      if(!catalog[gear.kind]?.[gear.key])throw new Error('未知装备');
      if(target!==null) {
        const slot=result.slots[target];
        if(!slot?.shikigami)throw new Error('御魂和纹章必须拖到式神身上');
        if(slot.equipment.length>=2)throw new Error('每名式神只有两个御魂 / 纹章槽位');
        if(slot.equipment.some(e=>e.kind===gear.kind&&e.key===gear.key))throw new Error('不能重复装备相同御魂或纹章');
        if(gear.kind==='emblem'&&catalog.shikigami[slot.shikigami].bonds.includes(catalog.emblem[gear.key].name.replace(/纹章$/,'')))throw new Error('式神已具有该羁绊');
        slot.equipment.push(clone(gear));
      }
      if(payload.kind==='equipped')result.slots[payload.from].equipment.splice(payload.equipment,1);
    }
    updateSpecialTargets(result,protector);
    return normalizeSpecials(result,catalog);
  }
  function startDrag(event,payload) {
    event.stopPropagation();state.drag=payload;
    event.dataTransfer.setData('application/x-chesslineup',JSON.stringify(payload));
    event.dataTransfer.effectAllowed=payload.kind==='unit'||payload.kind==='equipped'?'move':'copy';
  }
  function endDrag() {state.drag=null;document.querySelectorAll('.drag-over').forEach(e=>e.classList.remove('drag-over'));}
  function drop(event,target) {
    event.preventDefault();event.stopPropagation();
    try {
      const payload=JSON.parse(event.dataTransfer.getData('application/x-chesslineup'));
      state.draft=applyDrop(state.draft,payload,target);
      if(target!==null)state.selected=target;
      markDirty();render();
    } catch(error) {toast(error.message,true);}
    endDrag();
  }
  function dropZone(node,target) {
    node.addEventListener('dragover',event=>{event.preventDefault();event.stopPropagation();node.classList.add('drag-over');});
    node.addEventListener('dragleave',event=>{if(!node.contains(event.relatedTarget))node.classList.remove('drag-over');});
    node.addEventListener('drop',event=>drop(event,target));
  }
  function gearElement(item,slotIndex,gearIndex) {
    const data=entry(item.kind,item.key),node=document.createElement('button');
    node.type='button';node.className='equipped';node.draggable=false;
    node.title=`${data.name} · 长按后拖回左侧移除`;node.setAttribute('aria-label',node.title);
    const image=document.createElement('img');image.src=data.image;image.alt=data.name;image.draggable=false;node.append(image);
    let timer,armed=false;
    const parentSlot=()=>node.closest('.slot');
    node.addEventListener('pointerdown',event=>{event.stopPropagation();if(parentSlot())parentSlot().draggable=false;timer=setTimeout(()=>{armed=true;node.draggable=true;node.classList.add('armed');},320);});
    const cancel=()=>{clearTimeout(timer);if(!state.drag){armed=false;node.draggable=false;node.classList.remove('armed');if(parentSlot())parentSlot().draggable=true;}};
    node.addEventListener('pointerup',cancel);node.addEventListener('pointercancel',cancel);node.addEventListener('pointerleave',()=>{if(!armed)cancel();});
    node.addEventListener('click',event=>event.stopPropagation());
    node.addEventListener('dragstart',event=>{if(!armed){event.preventDefault();event.stopPropagation();return;}startDrag(event,{kind:'equipped',from:slotIndex,equipment:gearIndex});});
    node.addEventListener('dragend',()=>{armed=false;node.draggable=false;node.classList.remove('armed');if(parentSlot())parentSlot().draggable=true;endDrag();});
    return node;
  }
  function renderLibrary() {
    const query=$('search').value.trim().toLowerCase();$('cards').replaceChildren();
    const items=state.catalog[state.kind].filter(e=>(!query||`${e.name} ${e.key} ${(e.bonds||[]).join(' ')}`.toLowerCase().includes(query))&&(state.kind!=='shikigami'||!state.cost||e.cost===state.cost));
    for(const item of items) {
      const node=document.createElement('div');node.className='library-card';node.draggable=true;node.tabIndex=0;
      node.title=[item.name,...(item.bonds||[])].join(' · ');
      const image=document.createElement('img');image.src=item.image;image.alt=item.name;image.draggable=false;image.loading='lazy';
      const name=document.createElement('small');name.textContent=item.name;node.append(image,name);
      node.addEventListener('dragstart',event=>startDrag(event,{kind:state.kind,key:item.key}));node.addEventListener('dragend',endDrag);
      $('cards').append(node);
    }
    if(state.kind==='soul'&&state.draft.slots.some(slot=>slot.shikigami==='yume_san_byakuzou')) {
      const protect=document.createElement('div');protect.className='special-library-token';protect.draggable=true;
      protect.title='守护之印 · 拖到式神身上，不占御魂 / 纹章槽位';
      const image=document.createElement('img');image.src=PROTECT_IMAGE;image.alt='守护之印';image.draggable=false;protect.append(image);
      const name=document.createElement('span');name.textContent='守护之印';protect.append(name);
      const hint=document.createElement('small');hint.textContent='白藏主专属 · 不占装备位';protect.append(hint);
      protect.addEventListener('dragstart',event=>startDrag(event,{kind:'protect'}));protect.addEventListener('dragend',endDrag);
      $('cards').prepend(protect);
    }
    $('costs').hidden=state.kind!=='shikigami';
  }
  function renderBoard() {
    $('front').replaceChildren();$('back').replaceChildren();
    state.draft.slots.forEach((slot,index)=>{
      const node=document.createElement('div');node.className=`slot ${slot.shikigami?'filled':'empty'}${state.selected===index?' selected':''}`;node.dataset.position=String(index+1);node.dataset.index=String(index);node.tabIndex=0;node.setAttribute('aria-label',`${index+1}号位 ${slot.shikigami?entry('shikigami',slot.shikigami).name:'空槽'}`);
      if(slot.shikigami) {
        node.draggable=true;const unit=entry('shikigami',slot.shikigami);
        const image=document.createElement('img');image.src=unit.image;image.alt=unit.name;image.draggable=false;
        const name=document.createElement('span');name.className='slot-name';name.textContent=unit.name;
        const stack=document.createElement('div');stack.className='equipment-stack';slot.equipment.forEach((e,j)=>stack.append(gearElement(e,index,j)));
        node.append(image,name,stack);
        if(state.draft.hakuzosu_protect_position===index+1) {
          const protect=document.createElement('span');protect.className='protect-marker';protect.draggable=true;protect.title='守护之印 · 不占两件装备位；拖回左侧可移除';
          const image=document.createElement('img');image.src=PROTECT_IMAGE;image.alt='守护之印';image.draggable=false;protect.append(image);
          protect.addEventListener('dragstart',event=>startDrag(event,{kind:'protect'}));protect.addEventListener('dragend',endDrag);node.append(protect);
        }
        node.addEventListener('dragstart',event=>startDrag(event,{kind:'unit',from:index}));node.addEventListener('dragend',endDrag);
      }
      if(state.draft.arakawa_goldfish_position===index+1) {
        node.classList.remove('empty');node.classList.add('goldfish-slot');
        const fish=document.createElement('div');fish.className='goldfish-token';fish.draggable=true;fish.title='金鱼 · 拖动到空槽位以指定位置，不计阵容人口';
        const image=document.createElement('img');image.src=state.catalog.emblem.find(item=>item.name==='荒川纹章').image;image.alt='金鱼（荒川纹章卡面）';image.draggable=false;fish.append(image);
        const name=document.createElement('span');name.className='special-card-label';name.textContent='金鱼';fish.append(name);
        const hint=document.createElement('small');hint.textContent='荒川召唤物 · 拖动定位';fish.append(hint);
        fish.addEventListener('dragstart',event=>startDrag(event,{kind:'goldfish'}));fish.addEventListener('dragend',endDrag);node.append(fish);
        if(slot.shikigami)node.classList.add('position-conflict');
      }
      const position=document.createElement('span');position.className='position';position.textContent=index+1;node.append(position);
      node.addEventListener('click',()=>{state.selected=index;renderBoard();renderDetails();});
      node.addEventListener('keydown',event=>{if(event.key==='Enter'){state.selected=index;renderBoard();renderDetails();}});
      dropZone(node,index);$(index<6?'front':'back').append(node);
    });
    $('population').textContent=state.draft.slots.filter(s=>s.shikigami).length;
  }
  function renderDetails() {
    const slot=state.draft.slots[state.selected],unit=slot?.shikigami?entry('shikigami',slot.shikigami):null;
    $('selected-name').textContent=unit?`${state.selected+1}号位 · ${unit.name}`:'选择一个式神';
    $('selected-bonds').textContent=unit?unit.bonds.join(' · '):'将御魂或纹章拖到式神卡片上';
    $('weight').disabled=!unit;$('weight').value=unit?slot.weight:'';$('equipment-detail').replaceChildren();
    if(unit)for(let i=0;i<2;i++) {
      if(slot.equipment[i])$('equipment-detail').append(gearElement(slot.equipment[i],state.selected,i));
      else {const e=document.createElement('span');e.className='gear-placeholder';e.textContent='＋';e.title='可再拖入一件御魂或纹章';$('equipment-detail').append(e);}
    }
  }
  function renderMeta() {
    $('name').value=state.draft.display_name;$('key').value=state.draft.key;
    $('key').readOnly=!!state.origin;$('rename').disabled=!state.origin;$('delete').disabled=!state.origin;
    $('promote').disabled=state.origin?.source==='builtin'||!state.draft.slots.some(slot=>slot.shikigami);
    $('primary-bond').value=state.draft.primary_bond;
    $('dirty').textContent=state.dirty?'未保存':'已保存';
    const counts=bondCounts(state.draft);
    $('special-help').textContent=(counts['荒川']||0)>=2
      ?(state.draft.arakawa_goldfish_position?`金鱼：${state.draft.arakawa_goldfish_position} 号位，可拖动到其他空槽位。`:'金鱼待补位：有空槽位时按 12 → 1 自动补上。')
      :'荒川达到 2 时自动补入金鱼；上阵白藏主后，可从御魂区拖取守护之印。';
    renderBonds();
  }
  function renderBonds() {
    const counts=bondCounts(state.draft);
    if(!counts[state.selectedBond])state.selectedBond=null;
    $('bond-summary').replaceChildren();
    Object.entries(counts).sort((a,b)=>b[1]-a[1]).forEach(([bond,count])=>{
      const node=document.createElement('button');node.className='bond-block';node.dataset.bond=bond;
      node.classList.toggle('selected',state.selectedBond===bond);node.classList.toggle('secondary',state.draft.secondary_bond===bond);
      const name=document.createElement('span');name.textContent=bond;
      const amount=document.createElement('strong');amount.textContent=count;node.append(name,amount);
      if(state.draft.secondary_bond===bond){const label=document.createElement('small');label.textContent='次要';node.append(label);}
      node.addEventListener('click',()=>{state.selectedBond=bond;renderBonds();});$('bond-summary').append(node);
    });
    $('selected-bond').textContent=state.selectedBond?`已选：${state.selectedBond}`:'点击羁绊以指定次要羁绊';
    $('force-secondary').disabled=!state.selectedBond;
    $('force-secondary').textContent=state.selectedBond&&state.draft.secondary_bond===state.selectedBond?'取消次要羁绊':'设为次要羁绊';
    $('clear-secondary').disabled=!state.draft.secondary_bond;
  }
  function render() {normalizeSpecials(state.draft);const scroll=$('cards').scrollTop;renderLibrary();$('cards').scrollTop=scroll;renderBoard();renderDetails();renderMeta();}
  function validDraft(data,allowEmpty=false) {
    if(!data||!Array.isArray(data.slots)||data.slots.length!==12)throw new Error('阵容需包含 12 个槽位');
    if(!/^[a-z][a-z0-9_]{0,47}$/.test(data.key||''))throw new Error('阵容标识需以小写字母开头，只含小写字母、数字和下划线');
    if(!data.display_name?.trim())throw new Error('请填写阵容名称');
    let count=0;const seen=new Set();
    for(const slot of data.slots) {
      if(!Array.isArray(slot.equipment)||slot.equipment.length>2)throw new Error('每名式神最多两件装备');
      if(!slot.shikigami){if(slot.equipment.length)throw new Error('空槽不能放置装备');continue;}
      if(!entry('shikigami',slot.shikigami))throw new Error('包含未知式神');
      if(seen.has(slot.shikigami))throw new Error('不能重复放置同名式神');seen.add(slot.shikigami);count++;
      if(!Number.isInteger(slot.weight)||slot.weight<0||slot.weight>99)throw new Error('上阵权重需为 0–99 的整数');
      const gear=new Set();for(const e of slot.equipment){if(!entry(e.kind,e.key))throw new Error('包含未知装备');const k=e.kind+':'+e.key;if(gear.has(k))throw new Error('不能重复装备');gear.add(k);}
    }
    if(count>10||(!allowEmpty&&count===0))throw new Error('阵容需要 1–10 名式神');
    normalizeSpecials(data);
    if(data.arakawa_goldfish_position&&data.slots[data.arakawa_goldfish_position-1]?.shikigami)throw new Error('金鱼位置已被式神占用，请拖到空槽位');
    return data;
  }
  async function loadPresets() {
    const data=await request('/lineups');state.presets=[];$('presets').replaceChildren(option('','打开已有阵容…'));
    for(const [type,list] of Object.entries(data))for(const preset of list){const id=`${type}:${preset.key}`;state.presets.push({id,preset});$('presets').append(option(id,`${type==='builtin'?'内置':'自定义'} · ${preset.display_name}`));}
  }
  async function save() {
    validDraft(state.draft);const source=state.origin?.source||'custom';
    const saved=await request('/lineups?source='+source,{method:'POST',body:JSON.stringify(state.draft)});
    state.draft=saved;state.origin={source,key:saved.key};markDirty(false);await loadPresets();$('presets').value=source+':'+saved.key;render();toast('阵容已保存；正在进行的对局使用原快照');return saved;
  }
  async function init() {
    try {
      state.catalog=await request('/catalog');for(const kind of ['shikigami','soul','emblem'])state.index[kind]=Object.fromEntries(state.catalog[kind].map(e=>[e.key,{...e,image:freshImage(e.image)}]));
      $('primary-bond').append(option('','自动'));for(const bond of state.catalog.bonds)$('primary-bond').append(option(bond,bond));
      for(const config of state.catalog.configs)$('config').append(option(config,config));
      try {const draft=JSON.parse(localStorage.getItem('chesslineup-draft-v1'));if(draft)state.draft=validDraft(draft,true);state.origin=JSON.parse(localStorage.getItem('chesslineup-origin-v1'));if(state.origin?.key!==state.draft.key)state.origin=null;}catch(_){state.origin=null;}
      await loadPresets();renderLibrary();render();
      $('tabs').addEventListener('click',event=>{const kind=event.target.dataset.kind;if(!kind)return;state.kind=kind;$('tabs').querySelectorAll('button').forEach(e=>e.classList.toggle('active',e.dataset.kind===kind));renderLibrary();});
      $('costs').addEventListener('click',event=>{if(event.target.dataset.cost===undefined)return;state.cost=Number(event.target.dataset.cost);$('costs').querySelectorAll('button').forEach(e=>e.classList.toggle('active',Number(e.dataset.cost)===state.cost));renderLibrary();});
      $('search').addEventListener('input',renderLibrary);dropZone($('library'),null);
      for(const [id,field] of [['name','display_name'],['key','key'],['primary-bond','primary_bond']])$(id).addEventListener('input',()=>{state.draft[field]=$(id).value;markDirty();});
      $('force-secondary').addEventListener('click',()=>{if(!state.selectedBond)return;state.draft.secondary_bond=state.draft.secondary_bond===state.selectedBond?'':state.selectedBond;markDirty();renderBonds();});
      $('clear-secondary').addEventListener('click',()=>{state.draft.secondary_bond='';markDirty();renderBonds();});
      $('weight').addEventListener('change',()=>{const value=Number($('weight').value);if(!Number.isInteger(value)||value<0||value>99){toast('上阵权重需为 0–99 的整数',true);renderDetails();return;}state.draft.slots[state.selected].weight=value;markDirty();});
      $('presets').addEventListener('change',()=>{const preset=state.presets.find(e=>e.id===$('presets').value);if(!preset)return;state.draft=clone(preset.preset);state.origin={source:preset.id.split(':')[0],key:state.draft.key};state.selected=null;markDirty(false);render();});
      $('new').addEventListener('click',()=>{state.draft=newLineup();state.origin=null;state.selected=null;$('presets').value='';markDirty();render();});
      $('save-as').addEventListener('click',()=>{state.draft.key=newLineup().key;state.draft.display_name=(state.draft.display_name+' 副本').slice(0,40);state.origin=null;$('presets').value='';markDirty();render();$('key').focus();toast('副本已准备好，点击保存后写入自定义阵容');});
      $('promote').addEventListener('click',async()=>{try{validDraft(state.draft);$('promote').disabled=true;const saved=await request('/lineups/promote',{method:'POST',body:JSON.stringify(state.draft)});state.draft=saved;state.origin={source:'builtin',key:saved.key};markDirty(false);await loadPresets();$('presets').value='builtin:'+saved.key;render();toast('已转为内置阵容，自定义原件仍保留');}catch(e){toast(e.message,true);}finally{renderMeta();}});
      $('rename').addEventListener('click',()=>{$('name').focus();$('name').select();toast('编辑名称后点击保存，或按回车完成重命名');});
      $('name').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();save().catch(e=>toast(e.message,true));}});
      $('delete').addEventListener('click',async()=>{if(!state.origin)return;try{await request(`/lineups/${state.origin.source}/${encodeURIComponent(state.origin.key)}`,{method:'DELETE'});state.draft=newLineup();state.origin=null;state.selected=null;await loadPresets();markDirty();render();toast('阵容已删除');}catch(e){toast(e.message,true);}});
      $('save').addEventListener('click',()=>save().catch(e=>toast(e.message,true)));
      $('apply').addEventListener('click',async()=>{try{if(!$('config').value)throw new Error('请选择脚本配置');const saved=await save();const response=await request('/apply',{method:'POST',body:JSON.stringify({key:saved.key,source:state.origin.source,script:$('config').value})});toast(response.message);}catch(e){toast(e.message,true);}});
      $('export').addEventListener('click',()=>{try{validDraft(state.draft);const blob=new Blob([JSON.stringify({version:2,lineups:[state.draft]},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=state.draft.key+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){toast(e.message,true);}});
      $('import').addEventListener('click',()=>$('import-file').click());
      $('import-file').addEventListener('change',async()=>{try{const file=$('import-file').files[0];if(!file)return;const parsed=JSON.parse(await file.text());if(parsed.version!==undefined&&(parsed.version!==2||!Array.isArray(parsed.lineups)||parsed.lineups.length!==1))throw new Error('请一次导入一套 version 2 阵容');state.draft=clone(validDraft(parsed.version===2?parsed.lineups[0]:parsed));state.origin=null;state.selected=null;markDirty();render();toast('已导入，保存后可应用至脚本');}catch(e){toast(e.message,true);}finally{$('import-file').value='';}});
    } catch(error) {toast(`加载失败：${error.message}`,true);}
  }
  window.ChessLineupEditor={applyDrop,newLineup,emptySlot,state,validDraft,bondCounts,normalizeSpecials};
  document.addEventListener('DOMContentLoaded',init);
})();
