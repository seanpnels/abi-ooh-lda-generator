"""ABI OOH Stage 6 nationwide beta: DMA-driven, state-sharded, review-only OSM screening.
No inventory is written to GitHub. Every outcome requires human review.
"""
import gzip, hashlib, html, io, json, math, time, re, unicodedata, tempfile
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
import folium
from folium.plugins import MarkerCluster
from branca.element import MacroElement, Template
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
from openpyxl.styles import PatternFill, Font
from shapely.geometry import Point, box, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree
from pyproj import CRS, Transformer

class LiveRuler(MacroElement):
    """Straight-line ruler with a live cursor preview; entirely browser-side."""
    _template=Template(r"""
    {% macro script(this, kwargs) %}
    (function(map) {
        var active=false, start=null, finished=false, line=null, label=null;
        var marks=L.layerGroup().addTo(map);
        var control=L.control({position:'topright'});
        var button, readout;
        function clearMeasure() {
            marks.clearLayers(); start=null; finished=false; line=null;
            if(label) { map.removeLayer(label); label=null; }
            readout.textContent=active ? 'Click a start point.' : 'Ruler off';
        }
        function update(point) {
            var meters=map.distance(start,point);
            var feet=meters/0.3048;
            var text=feet.toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:1})+' ft';
            if(!line) line=L.polyline([start,point],{color:'#00d4c0',weight:3,dashArray:'6 5',interactive:false}).addTo(marks);
            else line.setLatLngs([start,point]);
            if(!label) label=L.tooltip({permanent:true,direction:'top',offset:[0,-12],className:'live-ruler-distance'}).setLatLng(point).setContent(text).addTo(map);
            label.setLatLng(point).setContent(text);
            readout.textContent=text+' · '+meters.toFixed(1)+' m';
        }
        control.onAdd=function() {
            var box=L.DomUtil.create('div','leaflet-bar');
            box.style.cssText='background:white;color:#17212b;padding:10px;border-radius:5px;min-width:220px;font:13px sans-serif;';
            button=L.DomUtil.create('button','',box);button.textContent='Ruler';
            button.type='button';button.style.cssText='cursor:pointer;margin-right:8px;';
            button.setAttribute('aria-pressed','false');
            var clear=L.DomUtil.create('button','',box);clear.textContent='Clear';clear.type='button';clear.style.cursor='pointer';
            readout=L.DomUtil.create('div','',box);readout.style.marginTop='7px';readout.textContent='Ruler off';
            var help=L.DomUtil.create('div','',box);help.style.cssText='margin-top:5px;max-width:240px;font-size:11px;color:#444;';
            help.textContent='Click start → move mouse → click end. Esc exits ruler mode.';
            L.DomEvent.disableClickPropagation(box);L.DomEvent.disableScrollPropagation(box);
            button.onclick=function() { setRulerActive(!active); };
            clear.onclick=clearMeasure;
            return box;
        };
        control.addTo(map);
        function setRulerActive(enabled) {
            active=enabled;
            button.textContent=active?'Ruler: ON':'Ruler';
            button.setAttribute('aria-pressed',String(active));
            map.getContainer().style.cursor=active?'crosshair':'';
            clearMeasure();
        }
        map.on('click',function(e) {
            if(!active)return;
            if(!start || finished) {
                clearMeasure();start=e.latlng;
                L.circleMarker(start,{radius:4,color:'#00d4c0',fill:true,fillOpacity:1,interactive:false}).addTo(marks);
                readout.textContent='Move mouse to measure; click to finish.';
            } else {
                update(e.latlng);finished=true;
                L.circleMarker(e.latlng,{radius:4,color:'#00d4c0',fill:true,fillOpacity:1,interactive:false}).addTo(marks);
            }
        });
        map.on('mousemove',function(e) { if(active && start && !finished)update(e.latlng); });
        var container=map.getContainer();
        if(!container.hasAttribute('tabindex'))container.setAttribute('tabindex','0');
        container.ownerDocument.addEventListener('keydown',function(e) { if(e.key==='Escape'&&active)setRulerActive(false); },true);
    })({{this._parent.get_name()}});
    {% endmacro %}
    """)
    def __init__(self):
        super().__init__()
        self._name='LiveRuler'


STATUS_LABELS={'RED':'Non-Compliant','YELLOW':'Needs Review','GREEN':'Compliant','GRAY':'Unable to Locate'}

def status_code(value):
    text=str(value)
    for code,label in STATUS_LABELS.items():
        if text==label or (code=='YELLOW' and text=='Requires Review') or text==code or text.startswith(code+' — '):return code
    return 'GRAY'


def display_status(value):
    return STATUS_LABELS[status_code(value)]


st.set_page_config(page_title='ABI OOH - LDA Screening Tool',page_icon='🗺️',layout='wide')
st.title('ABI OOH - LDA Screening Tool')
st.caption('209 DMA Source: Esri')
osm_source = st.empty()
osm_source.caption('Non-Compliant Location Source: OpenStreetMap (OSM)')
st.markdown('**Instructions:**\n\n1. Upload the MediaX Responses grid\n2. Check DMAs; add any missing\n3. Run Nationwide Screening\n4. Enjoy!')
st.warning('**Every placement requires human review.** OSM coverage is incomplete; candidate polygons are not verified building footprints. Results are preliminary screening outcomes, not final clearance.')
try:
    TOKEN=st.secrets.get('GITHUB_TOKEN','')
    REPO=st.secrets.get('GITHUB_REPO','seanpnels/abi-lda-data-poc')
except Exception:TOKEN='';REPO='seanpnels/abi-lda-data-poc'
if not TOKEN:st.error('Set GITHUB_TOKEN in Streamlit Secrets.');st.stop()
HEAD={'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}

@st.cache_data(ttl=900,show_spinner=False)
def releases(repo,token):
    h={**HEAD,'Authorization':f'Bearer {token}'}
    out=[]
    for page in range(1,11):
        r=requests.get(f'https://api.github.com/repos/{repo}/releases',params={'per_page':100,'page':page},headers=h,timeout=35)
        r.raise_for_status(); batch=r.json();out.extend(batch)
        if len(batch)<100:break
    return out

@st.cache_data(ttl=3600,show_spinner=False)
def asset_bytes(repo,asset_id,token):
    h={**HEAD,'Authorization':f'Bearer {token}','Accept':'application/octet-stream'}
    r=requests.get(f'https://api.github.com/repos/{repo}/releases/assets/{asset_id}',headers=h,timeout=(20,180))
    r.raise_for_status()
    if 'json' in r.headers.get('Content-Type',''):
        raise RuntimeError('GitHub returned JSON instead of release asset; check token permissions')
    return r.content

def latest_asset(all_releases,prefix,filename):
    for rel in all_releases:
        if rel.get('draft') or rel.get('prerelease') or not rel.get('tag_name','').startswith(prefix):continue
        assets={a['name']:a['id'] for a in rel.get('assets',[])}
        if filename in assets and 'manifest.json' in assets:
            return {'tag':rel['tag_name'],'file_id':assets[filename],'manifest_id':assets['manifest.json']}
    return None

def osm_refresh_label(all_releases, now):
    """Report the oldest GitHub publication among the snapshots the app would select."""
    current = {}
    for release in all_releases:
        match = re.fullmatch(r'stage6-osm-([a-z-]+)-[0-9]+', release.get('tag_name', ''))
        if not match or release.get('draft') or release.get('prerelease'):
            continue
        state = match[1]
        if state in current:
            continue
        assets = {asset['name'] for asset in release.get('assets', [])}
        if {'facilities.geojsonseq.gz', 'manifest.json'} <= assets:
            current[state] = release.get('published_at')
    if not current or not all(current.values()):
        return 'Refresh age unavailable'
    try:
        oldest = min(datetime.fromisoformat(value.replace('Z', '+00:00')) for value in current.values())
        hours = max(0, int((now - oldest).total_seconds() // 3600))
    except (TypeError, ValueError):
        return 'Refresh age unavailable'
    if hours < 1:
        age = 'less than an hour ago'
    else:
        amount, unit = (hours, 'hour') if hours < 24 else (hours // 24, 'day')
        age = f'{amount} {unit}{"s" if amount != 1 else ""} ago'
    return f'Oldest state snapshot published {age}'


def latest_index(all_releases):
    for rel in all_releases:
        if rel.get('draft') or not rel.get('tag_name','').startswith('stage6-index-'):continue
        for a in rel.get('assets',[]):
            if a['name']=='dma-state-index.json':return a['id']
    return None

def normalized_market(value):
    text=unicodedata.normalize('NFKD',str(value)).encode('ascii','ignore').decode().lower()
    text=re.sub(r'\bd\.\s*c\.?', 'dc', text)
    text=re.sub(r'\bsaint\b','st',text)
    text=re.sub(r'\bft\.?\b','fort',text)
    text=re.sub(r'\b(designated market area|dma|market)\b',' ',text)
    return re.sub(r'[^a-z0-9]+',' ',text).strip()


def market_column_names(frame):
    priority=['dmacode','dmaid','dmanumber','dma','dmaname','designatedmarketarea',
              'designatedmarketareaname','market','marketname','mediamarket','marketdma']
    ranking={name:i for i,name in enumerate(priority)}
    columns=[(ranking[key],i,c) for i,c in enumerate(frame.columns)
             if (key:=re.sub(r'[^a-z0-9]','',str(c).lower())) in ranking]
    return [c for _,_,c in sorted(columns)]


def read_inventory_grids(files):
    frames=[];all_columns=set()
    for upload in files:
        blob=io.BytesIO(upload.getvalue())
        frame=(pd.read_excel(blob,dtype=object) if upload.name.lower().endswith('.xlsx')
               else pd.read_csv(blob,dtype=object,encoding='utf-8-sig'))
        if frame.empty:raise ValueError(f'{upload.name}: empty inventory')
        def find(names):return next((c for c in frame.columns if str(c).strip().lower() in names),None)
        lat=find({'latitude','lat'});lon=find({'longitude','lon','lng','long'})
        if lat is None or lon is None:raise ValueError(f'{upload.name}: Latitude and Longitude columns required')
        frames.append((upload.name,frame,lat,lon,market_column_names(frame)))
        all_columns.update(frame.columns)
    def unique(base):
        name=base;n=2
        while name in all_columns:name=f'{base}_{n}';n+=1
        all_columns.add(name);return name
    meta={key:unique(base) for key,base in [('file','source_grid'),('row','source_excel_row'),
          ('lat','screening_latitude'),('lon','screening_longitude'),
          ('market','source_market'),('market_column','source_market_column')]}
    combined=[];summaries=[]
    for filename,frame,lat,lon,market_columns in frames:
        frame=frame.copy()
        frame[meta['file']]=filename;frame[meta['row']]=range(2,len(frame)+2)
        frame[meta['lat']]=frame[lat];frame[meta['lon']]=frame[lon]
        values=[];origins=[]
        for _,row in frame.iterrows():
            candidates=[c for c in market_columns if pd.notna(row[c]) and str(row[c]).strip()]
            column=candidates[0] if candidates else None
            values.append(str(row[column]).strip() if column is not None else '')
            origins.append(str(column) if column is not None else '')
        frame[meta['market']]=values;frame[meta['market_column']]=origins
        combined.append(frame)
        summaries.append({'Grid':filename,'Placements':len(frame),'Market columns':', '.join(map(str,market_columns)) or 'None found'})
    return pd.concat(combined,ignore_index=True,sort=False),meta,summaries


def suggest_market_dmas(values,names):
    aliases={}
    def add(alias,code):
        key=normalized_market(alias)
        if key:aliases.setdefault(key,set()).add(code)
    for code,name in names.items():
        add(name,code)
        # Common grids abbreviate the official DMA to its first city.
        primary=re.split(r'[-–—]',name.split(',')[0])[0].strip()
        add(primary,code)
        # Preserve state-qualified city names used in shorter market labels.
        state_match=re.search(r',\s*([A-Z]{2})(?=[^A-Za-z]|$)',name)
        if state_match:add(primary+' '+state_match.group(1),code)
    matched=set();review=[]
    for value,count in values.fillna('').astype(str).value_counts(dropna=False).items():
        raw=value.strip();key=normalized_market(raw)
        numeric=re.fullmatch(r'(?:dma\s*)?(\d{3})(?:\.0)?',raw,re.I)
        if numeric and numeric.group(1) in names:
            candidates={numeric.group(1)}
        else:
            code_suffix=re.search(r'\((\d{3})\)\s*$',raw)
            if code_suffix and code_suffix.group(1) in names:
                candidates={code_suffix.group(1)}
            else:candidates=aliases.get(key,set())
        if len(candidates)==1:
            code=next(iter(candidates));matched.add(code)
            review.append({'Grid market':raw or '(blank)','Placements':int(count),'Detected DMA':names[code],'Match':'Matched'})
        elif candidates:
            review.append({'Grid market':raw,'Placements':int(count),'Detected DMA':'; '.join(names[k] for k in sorted(candidates)),'Match':'Ambiguous — select manually'})
        else:
            review.append({'Grid market':raw or '(blank)','Placements':int(count),'Detected DMA':'','Match':'No match — select manually'})
    return matched,review


def reset_campaign():
    next_epoch=st.session_state.get('stage6_upload_epoch',0)+1
    for key in list(st.session_state):
        if str(key).startswith(('stage6_','campaign_panel_','google_review_','dma_picker_','dma_distance_')):
            del st.session_state[key]
    st.session_state['stage6_upload_epoch']=next_epoch


def reset_control():
    st.button('Reset / Start Over',key='stage6_reset_button',on_click=reset_campaign)


def show_osm_refresh():
    try:
        all_releases = releases(REPO, TOKEN)
        label = osm_refresh_label(all_releases, datetime.now(timezone.utc))
    except requests.RequestException:
        all_releases = None
        label = 'Refresh age unavailable'
    osm_source.caption(f'Non-Compliant Location Source: OpenStreetMap (OSM) · {label}')
    return all_releases


# Upload first; all grids are combined without dropping repeated placements.
files=st.file_uploader('1. Upload placement grids (CSV or XLSX)',type=['csv','xlsx'],accept_multiple_files=True,key='stage6_files_'+str(st.session_state.get('stage6_upload_epoch',0)))
if not files:
    st.info('Upload one or more grids to begin.')
    show_osm_refresh()
    st.stop()


@st.cache_resource(show_spinner=False, max_entries=1)
def geographic_index(repo,asset_id,token):
    # Parse the large national index once per published version, not per rerun.
    data=json.loads(asset_bytes(repo,asset_id,token))
    dmas=data['dma']['features'];states=data['states']['features']
    if len(dmas)!=209 or len(states)!=51:raise ValueError('Incomplete DMA/state index')
    return ({str(f['properties']['id']):f for f in dmas},
            {f['properties']['slug']:shape(f['geometry']) for f in states},
            {f['properties']['slug']:f['properties']['name'] for f in states})

@st.cache_resource(show_spinner=False, max_entries=209)
def selected_dma_geometry(repo,asset_id,token,code):
    features,_,_=geographic_index(repo,asset_id,token)
    return shape(features[code]['geometry'])

with st.spinner('Reading your grids and detecting markets…'):
    rels = show_osm_refresh()
    try:
        df,inventory_meta,grid_summaries=read_inventory_grids(files)
        lat_col=inventory_meta['lat'];lon_col=inventory_meta['lon']
    except Exception as exc:
        st.error(f'Inventory error: {exc}');st.stop()
    grid_digest=hashlib.sha256()
    for upload in files:
        grid_digest.update(upload.name.encode('utf-8'));grid_digest.update(hashlib.sha256(upload.getvalue()).digest())
    inventory_key=grid_digest.hexdigest()
    try:
        if rels is None:
            st.error('Cannot load Stage 6 releases from GitHub.');st.stop()
        idx_id=latest_index(rels)
        if idx_id is None:
            st.error('Stage 6 DMA index not published yet.');st.stop()
        dma_features,state_geoms,state_names=geographic_index(REPO,idx_id,TOKEN)
    except Exception as exc:
        st.error(f'Cannot load Stage 6 geographic index: {exc}');st.stop()
    dma_names={code:f['properties']['name'] for code,f in dma_features.items()}
    labels={f'{dma_names[k]} ({k})':k for k in sorted(dma_names,key=lambda k:dma_names[k])}
    suggested_codes,market_review=suggest_market_dmas(df[inventory_meta['market']],dma_names)

st.caption(f'{len(files)} grid(s) loaded: {len(df):,} placements total. Source filenames are included in exports.')
with st.expander('Uploaded grids'):
    st.dataframe(pd.DataFrame(grid_summaries),hide_index=True)

MARKET_PICKER_HTML=r'''<!doctype html><html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;background:transparent;font:14px Arial,sans-serif;color:var(--text,#eef3f8)}
.panel{background:linear-gradient(135deg,#182638,#101b29);border:1px solid #334155;border-top:3px solid #2dd4a6;border-radius:14px;padding:18px 20px;color:#eef3f8}
.heading{font-size:16px;font-weight:700}.sub{color:#aebdcd;font-size:12px;margin:6px 0 14px}
#selected{max-height:330px;overflow:auto}.market-row{display:flex;align-items:center;gap:14px;padding:10px 0;border-bottom:1px solid #33415588}
.market-name{flex:1;min-width:0;font-size:14px;font-weight:500;line-height:1.4}.distance-group{display:flex;border:1px solid #46566b;border-radius:7px;overflow:hidden;flex-shrink:0}
.distance-group button{font:inherit;padding:7px 12px;background:transparent;color:#b9c6d5;border:0;cursor:pointer;min-width:60px}
.distance-group button.active{background:#20bda9;color:#052820;font-weight:700}.remove{background:transparent;color:#9eafc3;border:0;font-size:21px;cursor:pointer;padding:3px 6px}.remove:hover{color:#ff777e}
#add{border:0;background:transparent;color:#2dd4a6;padding:12px 0 5px;font:inherit;cursor:pointer;text-align:left}
#searchbox{margin-top:10px}#searchbox[hidden]{display:none}
#search{display:block;width:100%;background:#101b29;color:#eef3f8;border:1px solid #526176;border-radius:7px;padding:10px;font:inherit;outline:none}
#search:focus{border-color:#2dd4a6}#results{margin-top:4px;max-height:165px;overflow:auto;background:#101b29;border:1px solid #334155;border-radius:7px}
.option{display:block;width:100%;background:transparent;color:#eef3f8;border:0;text-align:left;padding:10px;font:inherit;cursor:pointer}
.option:hover,.option:focus{background:#20364b}#empty{color:#aebdcd;font-size:13px;padding:12px 0}
.footer{display:flex;flex-direction:column;align-items:flex-start;gap:12px;margin-top:15px}.footer span{font-size:12px;color:#aebdcd}
#apply{background:#20bda9;color:#052820;border:0;border-radius:7px;padding:10px 16px;font:600 13px Arial,sans-serif;cursor:pointer}#apply:disabled{opacity:.45;cursor:default}
@media(max-width:520px){.panel{padding:14px}.market-row{gap:6px}.market-name{font-size:12px}.distance-group button{padding:7px 8px;min-width:48px}}
</style></head><body><div class="panel" id="panel"><div class="heading">Campaign markets &amp; distances</div><div class="sub">Confirm your detected markets. Use 500′ by default or select a 250′ exception.</div><div id="selected"></div><button id="add" type="button">+ Add market</button><div id="searchbox" hidden><input id="search" type="search" aria-label="Search markets" placeholder="Type a market name…" autocomplete="off"><div id="results" role="listbox" aria-label="Available markets"></div></div><div class="footer"><span id="summary"></span><button id="apply" type="button">Apply campaign settings</button></div></div>
<script>
let options=[],rules={},initialized=false,lastHeight=0;
const selected=document.getElementById('selected'),add=document.getElementById('add'),searchbox=document.getElementById('searchbox'),search=document.getElementById('search'),results=document.getElementById('results'),summary=document.getElementById('summary'),apply=document.getElementById('apply');
function send(type,extra){window.parent.postMessage(Object.assign({isStreamlitMessage:true,type:type},extra||{}),'*');}
function resize(){const height=Math.ceil(document.getElementById('panel').getBoundingClientRect().height)+6;if(height!==lastHeight){lastHeight=height;send('streamlit:setFrameHeight',{height:height});}}
function filter(){
 results.replaceChildren();const query=search.value.trim().toLowerCase();
 const matches=options.filter(option=>!(option.value in rules)&&option.label.toLowerCase().includes(query));
 matches.forEach(option=>{const button=document.createElement('button');button.type='button';button.className='option';button.setAttribute('role','option');button.textContent=option.label;button.onclick=()=>{rules[option.value]=500;search.value='';searchbox.hidden=true;add.textContent='+ Add market';render();};results.appendChild(button);});
 if(!matches.length){const empty=document.createElement('div');empty.className='option';empty.textContent='No matching markets';results.appendChild(empty);}
 resize();
}
function render(){
 selected.replaceChildren();
 options.filter(option=>option.value in rules).forEach(option=>{
  const row=document.createElement('div');row.className='market-row';
  const name=document.createElement('div');name.className='market-name';name.textContent=option.label;row.appendChild(name);
  const group=document.createElement('div');group.className='distance-group';group.setAttribute('role','group');group.setAttribute('aria-label',option.label+' distance');
  [500,250].forEach(distance=>{const button=document.createElement('button');button.type='button';button.className=rules[option.value]===distance?'active':'';button.textContent=distance+'′';button.setAttribute('aria-pressed',String(rules[option.value]===distance));button.onclick=()=>{rules[option.value]=distance;render();};group.appendChild(button);});row.appendChild(group);
  const remove=document.createElement('button');remove.className='remove';remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','Remove '+option.label);remove.onclick=()=>{delete rules[option.value];render();};row.appendChild(remove);selected.appendChild(row);
 });
 if(!Object.keys(rules).length){const empty=document.createElement('div');empty.id='empty';empty.textContent='No markets selected. Add a market below.';selected.appendChild(empty);}
 const count=Object.keys(rules).length;summary.textContent=count+' market'+(count===1?'':'s')+' selected';apply.disabled=count===0;
 if(!searchbox.hidden)filter();else resize();
}
add.onclick=()=>{searchbox.hidden=!searchbox.hidden;add.textContent=searchbox.hidden?'+ Add market':'− Close search';if(!searchbox.hidden){filter();search.focus();}resize();};
search.addEventListener('input',filter);
search.addEventListener('keydown',event=>{if(event.key==='Escape'){searchbox.hidden=true;add.textContent='+ Add market';resize();}if(event.key==='Enter'){event.preventDefault();const first=results.querySelector('button');if(first)first.click();}});
apply.onclick=()=>send('streamlit:setComponentValue',{value:{distances:Object.assign({},rules),revision:Date.now()},dataType:'json'});
window.addEventListener('message',event=>{
 if(event.source!==window.parent||!event.data||event.data.type!=='streamlit:render')return;
 if(!initialized){options=event.data.args.options||[];const initial=event.data.args.initial_rules||{};options.forEach(option=>{if(option.value in initial)rules[option.value]=initial[option.value]===250?250:500;});initialized=true;render();}else resize();
});
window.addEventListener('resize',resize);
send('streamlit:componentReady',{apiVersion:1});
</script></body></html>'''


@st.cache_resource(show_spinner=False)
def market_picker_component():
    directory=Path(tempfile.mkdtemp(prefix='lda_market_picker_'))
    (directory/'index.html').write_text(MARKET_PICKER_HTML,encoding='utf-8')
    return components.declare_component('lda_campaign_panel_v2',path=str(directory))


@st.cache_resource(show_spinner=False, max_entries=1)
def nationwide_dma_lookup(repo, asset_id, token):
    features, _, _ = geographic_index(repo, asset_id, token)
    codes = tuple(features)
    geometries = [shape(features[code]['geometry']) for code in codes]
    return codes, STRtree(geometries)


class OutsideSelectedDMA(ValueError):
    pass


def placement_dma_rule(lon,lat,chosen,geometries,distances,names):
    matches=[code for code in chosen if geometries[code].covers(Point(lon,lat))]
    if not matches:raise OutsideSelectedDMA('Outside selected DMA boundary')
    return {'dma':'; '.join(names[code] for code in matches),
            'distance':max(distances[code] for code in matches),
            'note':'Multiple DMA boundaries matched; larger distance applied.' if len(matches)>1 else ''}


suggested_labels=[label for label,code in labels.items() if code in suggested_codes]
settings_context=(inventory_key,idx_id,st.session_state.get('stage6_upload_epoch',0))
if st.session_state.get('stage6_inventory_context')!=settings_context:
    # New grids or a newly published boundary index must invalidate old settings/results.
    st.session_state.pop('stage6_settings',None)
    st.session_state['stage6_dma_selection']=suggested_labels
    st.session_state['stage6_dma_distances']={code:500 for code in dma_names}
    st.session_state['stage6_inventory_context']=settings_context
st.caption(f'Automatically selected {len(suggested_labels)} DMA(s) from grid market columns. Confirm or edit below.')
unmatched=[r for r in market_review if r['Match']!='Matched']
if unmatched:
    count=sum(r['Placements'] for r in unmatched)
    st.warning(f'{count:,} placement(s) have missing, ambiguous or unrecognized market names. Review the matches and select any additional DMAs manually.')
with st.expander('Review market detection',expanded=bool(unmatched)):
    st.dataframe(pd.DataFrame(market_review),hide_index=True)
    st.caption('Matching uses market names/codes, not geocoded addresses. Placement coordinates are still checked against your confirmed DMA selection.')

st.markdown('**2. Campaign settings**')
initial_selection=st.session_state.get('stage6_dma_selection',suggested_labels)
initial_rules={labels[label]:st.session_state['stage6_dma_distances'].get(labels[label],500)
               for label in initial_selection if label in labels}
panel_value=market_picker_component()(
    options=[{'value':code,'label':dma_names[code]} for code in labels.values()],
    initial_rules=initial_rules,
    key='campaign_panel_v2_'+hashlib.sha256(str(settings_context).encode()).hexdigest()[:16]
        +'_'+str(st.session_state.get('stage6_panel_revision',0)),default=None)
if panel_value is not None:
    try:
        proposed=panel_value['distances']
        if not isinstance(proposed,dict) or not proposed:raise ValueError('Select at least one market.')
        if any(code not in dma_names or type(value) is not int or value not in (250,500)
               for code,value in proposed.items()):raise ValueError('Invalid market distance settings.')
        selected_labels=[label for label,code in labels.items() if code in proposed]
        dma_distances={labels[label]:proposed[labels[label]] for label in selected_labels}
        st.session_state['stage6_dma_selection']=selected_labels
        st.session_state['stage6_dma_distances'].update(dma_distances)
        st.session_state['stage6_settings']={'labels':selected_labels,'distances':dma_distances}
    except (KeyError,TypeError,ValueError) as exc:
        st.error(f'Campaign settings: {exc}');st.stop()
if 'stage6_settings' not in st.session_state:
    st.info('Confirm your markets and distances, then click Apply campaign settings.');reset_control();st.stop()
applied=st.session_state['stage6_settings']
selected_labels=applied['labels'];dma_distances=applied['distances']
st.caption('Applied distances: '+ ' · '.join(f'{dma_names[code]}: {distance} ft' for code,distance in dma_distances.items()))
st.caption('Each placement uses its coordinate-matched DMA distance plus a 50 ft review band.')
dma_geoms={labels[label]:selected_dma_geometry(REPO,idx_id,TOKEN,labels[label]) for label in selected_labels}

AMENITIES={'school':'school','kindergarten':'daycare','childcare':'daycare','place_of_worship':'worship','college':'university','university':'university'}
BUILDINGS={'school':'school','kindergarten':'daycare','church':'worship','chapel':'worship','mosque':'worship','synagogue':'worship','temple':'worship','college':'university','university':'university'}
def classify(t):
    cats=set()
    if t.get('amenity') in AMENITIES:cats.add(AMENITIES[t['amenity']])
    if t.get('building') in BUILDINGS:cats.add(BUILDINGS[t['building']])
    if t.get('social_facility') in ('drug_addiction','alcohol_addiction'):cats.add('substance_use_candidate')
    name = str(t.get('name') or '')
    school_name = re.search(
        r'\b(?:elementary|primary|middle|junior high|senior high|high|secondary|grade) school\b'
        r'|\b(?:preschool|kindergarten|day[ -]?care|child[ -]?care|college prep(?:aratory)?)\b',
        name, re.I)
    if school_name and cats & {'school', 'university'}:
        cats.discard('university')
        cats.add('school')
    elif 'daycare' not in cats and (
        'university' in cats or ('school' in cats and re.search(r'\b(?:college|university)\b', name, re.I))
    ):
        cats.discard('school')
        cats.add('university')
    return sorted(cats)

def local_projection(lon,lat):
    crs=CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs')
    return Transformer.from_crs('EPSG:4326',crs,always_xy=True).transform

# The 1km state coverage search is intentionally greater than the 550ft max buffer.
# State geometries are simplified for selection only; uncertainty at boundaries is flagged.
def needed_states(lon,lat):
    p=Point(lon,lat); near=[]
    for slug,g in state_geoms.items():
        if not box(lon-.025,lat-.025,lon+.025,lat+.025).intersects(g):continue
        project=local_projection(lon,lat)
        if transform(project,g).distance(Point(0,0))<=1000:near.append(slug)
    return near

def dma_match(lon,lat,chosen):
    p=Point(lon,lat)
    return any(dma_geoms[k].covers(p) for k in chosen)

@st.cache_resource(show_spinner=False)
def load_filtered_state(repo,asset_id,manifest_id,token,bbox_tuple):
    """Only retain features near uploaded placements; do not index entire large states."""
    manifest=json.loads(asset_bytes(repo,manifest_id,token))
    blob=asset_bytes(repo,asset_id,token)
    if hashlib.sha256(blob).hexdigest()!=manifest.get('sha256'):
        raise RuntimeError('Dataset SHA256 does not match manifest; rejecting partial/mismatched snapshot')
    area=box(*bbox_tuple); features=[];errors=0;total=0
    with gzip.open(io.BytesIO(blob),'rt',encoding='utf-8') as f:
        for line in f:
            line=line.strip().lstrip('\x1e')
            if not line:continue
            try:
                obj=json.loads(line);props=obj.get('properties') or {};tags=props.get('tags') or props
                cats=classify(tags)
                if not cats or not obj.get('geometry'):continue
                total+=1
                geom=shape(obj['geometry'])
                if geom.is_empty or not geom.intersects(area):continue
                if not geom.is_valid:geom=geom.buffer(0)
                if geom.is_empty:continue
                quality='polygon_unverified' if geom.geom_type in ('Polygon','MultiPolygon') else 'point_or_line_unverified'
                features.append({'geom':geom,'name':str(tags.get('name','')),'cats':cats,
                    'osm_id':str(props.get('@id',obj.get('id',''))),'quality':quality})
            except Exception:errors+=1
    if errors or total<100:raise RuntimeError(f'Invalid dataset: {total} candidates, {errors} parse errors')
    return features,STRtree([f['geom'] for f in features]) if features else None,manifest

def facility_pin_location(geometry):
    """Use the mapped point, or an interior point of the mapped facility area."""
    if geometry.is_empty:
        return None, None
    point=geometry if geometry.geom_type=='Point' else geometry.representative_point()
    return float(point.y), float(point.x)


def facility_triggers_red(distance, facility, restriction_ft):
    return (facility['quality'] == 'polygon_unverified'
            and facility['cats'] != ['university']
            and distance <= restriction_ft * .3048)


def placement_pin_title(row, number):
    fields = {re.sub(r'[^a-z0-9]', '', str(key).lower()): value for key, value in row.items()}
    for key in ('unit', 'unitnumber', 'unitno', 'unitid', 'mediapackagename',
                'medianame', 'packagename', 'mediapackage', 'package', 'media'):
        value = fields.get(key)
        if pd.notna(value) and str(value).strip():
            return str(value).strip()
    return f'Placement {number}'


def additional_college_pins(map_rows, facility_hits):
    primary = {(str(row['osm_id']), float(row['facility_latitude']), float(row['facility_longitude']))
               for _, _, row in map_rows[:1000]
               if status_code(row['screening_status']) in ('RED', 'YELLOW')
               and pd.notna(row['facility_latitude']) and pd.notna(row['facility_longitude'])}
    records = []
    for hit in facility_hits:
        if hit['status'] != 'YELLOW' or hit['category'] != 'university':
            continue
        key = (str(hit['osm_id']), float(hit['latitude']), float(hit['longitude']))
        if key in primary:
            continue
        primary.add(key)
        osm_id, lat, lon = key
        title = hit['name'].strip() or 'Unnamed OSM site'
        records.append({
            'position': {'lat': lat, 'lng': lon}, 'title': title,
            'popup': f'<b>{html.escape(title)}</b><br>Possible college or university — verify.'
                     f'<br>OSM: {html.escape(osm_id)}',
        })
    return records


def dma_osm_pin_records(map_rows):
    area = unary_union(list(dma_geoms.values()))
    within_dma = prep(area)
    seen = set()
    for _, _, row in map_rows[:1000]:
        if status_code(row['screening_status']) not in ('RED', 'YELLOW'):
            continue
        lat, lon = row.get('facility_latitude'), row.get('facility_longitude')
        if pd.notna(lat) and pd.notna(lon):
            seen.add((round(float(lat), 7), round(float(lon), 7)))
    records = []
    unavailable = []
    for slug, state in state_geoms.items():
        if not within_dma.intersects(state):
            continue
        release = latest_asset(rels, f'stage6-osm-{slug}-', 'facilities.geojsonseq.gz')
        if not release:
            unavailable.append(state_names[slug])
            continue
        try:
            # Use the full DMA envelope so generalized state edges don't clip pins.
            facilities, _, manifest = load_filtered_state(
                REPO, release['file_id'], release['manifest_id'], TOKEN, area.bounds)
            generated = datetime.fromisoformat(str(manifest['generated_utc']).replace('Z', '+00:00'))
            if (datetime.now(timezone.utc) - generated).total_seconds() > 10 * 86400:
                raise ValueError('Stale snapshot')
        except Exception:
            unavailable.append(state_names[slug])
            continue
        for site in facilities:
            lat, lon = facility_pin_location(site['geom'])
            if lat is None or not within_dma.covers(Point(lon, lat)):
                continue
            position = (round(lat, 7), round(lon, 7))
            if position in seen:
                continue
            seen.add(position)
            title = site['name'].strip() or 'Unnamed OSM site'
            popup = (f'<b>{html.escape(title)}</b><br>'
                     + html.escape(', '.join(site['cats']))
                     + '<br>OSM: ' + html.escape(site['osm_id']))
            records.append({'position': {'lat': lat, 'lng': lon}, 'title': title, 'popup': popup})
    return records, unavailable


def google_review_map_records(rows):
    """Render one Google map; markers and ruler operate within its browser iframe."""
    records=[]
    colors={'RED':'#d93025','YELLOW':'#f9ab00','GREEN':'#188038','GRAY':'#80868b'}
    for number,(lat,lon,row) in enumerate(rows[:1000],start=1):
        flag=status_code(row['screening_status']);status=display_status(row['screening_status'])
        title=placement_pin_title(row,number)
        popup=(f'<b>{html.escape(title)}</b><br>'+html.escape(status)
               +'<br>Distance: '+html.escape(str(row['distance_ft']))+' ft'
               +'<br>Facility: '+html.escape(str(row['nearby_facility'])[:150])
               +'<br>DMA: '+html.escape(str(row.get('matched_dma','')))
               +'<br>Applied distance: '+html.escape(str(row.get('applied_distance_ft','')))+' ft')
        record={'position':{'lat':float(lat),'lng':float(lon)},'color':colors.get(flag,colors['GRAY']),
                'title':f'{title}: {status}','popup':popup,'facility':None}
        facility_lat=row.get('facility_latitude');facility_lon=row.get('facility_longitude')
        if flag in ('RED','YELLOW') and pd.notna(facility_lat) and pd.notna(facility_lon):
            name=str(row['nearby_facility']).strip() or 'Unnamed mapped facility'
            record['facility']={'position':{'lat':float(facility_lat),'lng':float(facility_lon)},
                'title':'Flagged facility: '+name,
                'popup':(f'<b>Flagged facility for placement {number}</b><br>'+html.escape(name)
                         +'<br>'+html.escape(str(row['facility_category']))
                         +'<br>OSM: '+html.escape(str(row['osm_id']))
                         +'<br>Mapped facility location; measure manually.')}
        records.append(record)
    return records



GOOGLE_REVIEW_COMPONENT_HTML=r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="referrer" content="strict-origin-when-cross-origin">
<style>
html,body{margin:0;padding:0;height:100%;font:13px Arial,sans-serif;color:#17212b}
#map{height:600px;width:100%} .toolbar{background:#fff;margin:10px;padding:10px;border-radius:5px;box-shadow:0 1px 5px #666;max-width:290px}
.toolbar button{cursor:pointer;margin-right:6px;padding:5px 9px}.toolbar label{display:inline-block;margin-top:8px;margin-right:8px}
#distance{font-weight:bold;margin-top:8px}.help{font-size:11px;margin-top:5px}
#message{position:absolute;bottom:32px;left:12px;z-index:10;background:white;padding:10px;max-width:420px;display:none;border:1px solid #b3261e}
.ruler-label{position:absolute;white-space:nowrap;background:white;border:1px solid #555;border-radius:4px;padding:5px;transform:translate(8px,-130%);pointer-events:none;font-weight:bold;color:#17212b}
</style></head><body>
<div id="map"></div><div id="message" role="alert"></div>
<script>
let records=[],osmSites=[],collegePins=[];
let componentStarted=false;
function showError(text){const box=document.getElementById('message');box.textContent=text;box.style.display='block';}
window.gm_authFailure=function(){showError('Google Maps rejected the key. Check Maps JavaScript API, billing and the allowed website. Keep website restrictions enabled.');};
window.initReviewMap=function(){
 try {
  const map=new google.maps.Map(document.getElementById('map'),{
   center:records[0].position,zoom:10,mapTypeId:'roadmap',streetViewControl:false,fullscreenControl:true,
   mapTypeControl:true,mapTypeControlOptions:{
    mapTypeIds:['roadmap','satellite'],
    style:google.maps.MapTypeControlStyle.HORIZONTAL_BAR,
    position:google.maps.ControlPosition.TOP_LEFT
   },
   clickableIcons:true,gestureHandling:'greedy',styles:[{featureType:'poi.business',stylers:[{visibility:'on'}]}]
  });
  const bounds=new google.maps.LatLngBounds();
  const info=new google.maps.InfoWindow();
  const placements=[],facilities=[];
  let measuring=false,start=null,finished=false,line=null,label=null,endDot=null,startDot=null;
  const toolbar=document.createElement('div');toolbar.className='toolbar';
  toolbar.innerHTML='<button id="ruler" type="button" aria-pressed="false">Ruler</button><button id="clear" type="button">Clear</button><div id="distance">Ruler off</div><div class="help">Click start → move mouse → click end. Esc exits ruler mode.</div><label><input id="placements" type="checkbox" checked> Placements</label><label><input id="facilities" type="checkbox" checked> Flagged facilities</label>';
  map.controls[google.maps.ControlPosition.TOP_RIGHT].push(toolbar);
  ['click','dblclick','mousedown','touchstart'].forEach(type=>toolbar.addEventListener(type,e=>e.stopPropagation()));
  const readout=toolbar.querySelector('#distance'),rulerButton=toolbar.querySelector('#ruler');
  class DistanceLabel extends google.maps.OverlayView {
   constructor(){super();this.position=null;this.text='';}
   onAdd(){this.div=document.createElement('div');this.div.className='ruler-label';this.getPanes().floatPane.appendChild(this.div);this.draw();}
   draw(){if(!this.div||!this.position)return;const p=this.getProjection().fromLatLngToDivPixel(this.position);if(p){this.div.style.left=p.x+'px';this.div.style.top=p.y+'px';this.div.textContent=this.text;}}
   onRemove(){if(this.div)this.div.remove();this.div=null;}
   update(point,text){this.position=point;this.text=text;this.draw();}
  }
  function clearMeasure(){
   [line,label,startDot,endDot].forEach(item=>{if(item)item.setMap(null);});
   line=label=startDot=endDot=null;start=null;finished=false;
   readout.textContent=measuring?'Click a start point.':'Ruler off';
  }
  function dot(point){return new google.maps.Marker({position:point,map,clickable:false,icon:{path:google.maps.SymbolPath.CIRCLE,scale:4,fillColor:'#00796b',fillOpacity:1,strokeWeight:1,strokeColor:'#fff'}});}
  function preview(point){
   const meters=google.maps.geometry.spherical.computeDistanceBetween(start,point);
   const text=(meters/0.3048).toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:1})+' ft';
   if(!line)line=new google.maps.Polyline({map,path:[start,point],geodesic:true,strokeColor:'#00796b',strokeWeight:3,clickable:false});
   else line.setPath([start,point]);
   if(!label){label=new DistanceLabel();label.update(point,text);label.setMap(map);}else label.update(point,text);
   readout.textContent=text+' · '+meters.toFixed(1)+' m';
  }
  function measurePoint(point){
   if(!point)return;
   if(!start||finished){clearMeasure();start=point;startDot=dot(point);readout.textContent='Move mouse to measure; click to finish.';}
   else{preview(point);endDot=dot(point);finished=true;}
  }
  function setRulerActive(enabled){
   measuring=enabled;info.close();clearMeasure();
   rulerButton.textContent=measuring?'Ruler: ON':'Ruler';rulerButton.setAttribute('aria-pressed',String(measuring));
   map.setOptions({draggableCursor:measuring?'crosshair':null});
  }
  rulerButton.onclick=()=>setRulerActive(!measuring);
  toolbar.querySelector('#clear').onclick=clearMeasure;
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&measuring)setRulerActive(false);},true);
  map.addListener('click',e=>{if(measuring){if(e.stop)e.stop();measurePoint(e.latLng);}});
  map.addListener('mousemove',e=>{if(measuring&&start&&!finished&&e.latLng)preview(e.latLng);});
  function attach(marker,popup){
   marker.addListener('click',()=>{if(measuring)measurePoint(marker.getPosition());else{info.setContent(popup);info.open({map,anchor:marker});}});
   marker.addListener('mousemove',e=>{if(measuring&&start&&!finished&&e.latLng)preview(e.latLng);});
  }
  const facilityPinPath='M 0,0 C -3,-5 -10,-12 -10,-18 A 10,10 0 1,1 10,-18 C 10,-12 3,-5 0,0 Z';
  const visibleSites=new Map();
  function updateOsmSites(){
   const viewport=map.getBounds();
   const zoom=map.getZoom();
   osmSites.forEach((site,index)=>{
    const visible=viewport && zoom>=12 && viewport.contains(site.position);
    const existing=visibleSites.get(index);
    if(!visible && existing){
     existing.setMap(null);google.maps.event.clearInstanceListeners(existing);visibleSites.delete(index);
    }else if(visible && !existing){
     const marker=new google.maps.Marker({position:site.position,map,title:site.title,zIndex:1,
      icon:{path:facilityPinPath,fillColor:'#111',fillOpacity:1,strokeColor:'#fff',strokeWeight:1,scale:1}});
     visibleSites.set(index,marker);attach(marker,site.popup);
    }
   });
  }
  if(osmSites.length)map.addListener('idle',updateOsmSites);
  records.forEach(record=>{
   bounds.extend(record.position);
   const marker=new google.maps.Marker({position:record.position,map,title:record.title,zIndex:10,
    icon:{path:google.maps.SymbolPath.CIRCLE,scale:6,fillColor:record.color,fillOpacity:0.9,strokeColor:'#fff',strokeWeight:1}});
   placements.push(marker);attach(marker,record.popup);
   if(record.facility){
    const facility=record.facility;
    const pin=new google.maps.Marker({position:facility.position,map,title:facility.title,zIndex:20,
     icon:{path:facilityPinPath,
      fillColor:record.color,fillOpacity:1,strokeColor:'#fff',strokeWeight:1,scale:1}});
    facilities.push(pin);attach(pin,facility.popup);bounds.extend(facility.position);
   }
  });
  collegePins.forEach(site=>{
   const pin=new google.maps.Marker({position:site.position,map,title:site.title,zIndex:19,
    icon:{path:facilityPinPath,fillColor:'#f9ab00',fillOpacity:1,strokeColor:'#fff',strokeWeight:1,scale:1}});
   facilities.push(pin);attach(pin,site.popup);
  });
  toolbar.querySelector('#placements').onchange=e=>placements.forEach(marker=>marker.setMap(e.target.checked?map:null));
  toolbar.querySelector('#facilities').onchange=e=>facilities.forEach(marker=>marker.setMap(e.target.checked?map:null));
  if(records.length>1){map.fitBounds(bounds,50);google.maps.event.addListenerOnce(map,'idle',()=>{if(map.getZoom()>17)map.setZoom(17);});}
  else map.setZoom(16);
 }catch(error){showError('Map initialization failed: '+error.message);console.error(error);}
};
function sendToStreamlit(type, extra) {
 window.parent.postMessage(Object.assign({isStreamlitMessage:true,type:type},extra||{}),'*');
}
window.addEventListener('message',function(event) {
 if(event.source!==window.parent || !event.data || event.data.type!=='streamlit:render')return;
 sendToStreamlit('streamlit:setFrameHeight',{height:620});
 if(componentStarted)return;
 const args=event.data.args||{};
 if(!Array.isArray(args.records)||!args.records.length)return;
 if(!args.api_key){showError('Google Maps key missing.');return;}
 componentStarted=true;records=args.records;osmSites=args.osm_sites||[];collegePins=args.college_pins||[];
 const loader=document.createElement('script');
 loader.src='https://maps.googleapis.com/maps/api/js?'+new URLSearchParams({key:args.api_key,v:'quarterly',loading:'async',libraries:'geometry',callback:'initReviewMap'}).toString();
 loader.async=true;loader.onerror=()=>showError('Google Maps could not load. Check your connection or browser content blocker.');document.head.appendChild(loader);
});
sendToStreamlit('streamlit:componentReady',{apiVersion:1});
sendToStreamlit('streamlit:setFrameHeight',{height:620});
</script></body></html>'''


@st.cache_resource(show_spinner=False)
def google_review_component():
    # Serve a real HTTP component URL. A srcdoc iframe cannot satisfy Google's
    # website referrer restrictions in Safari (it reports about:srcdoc).
    directory=Path(tempfile.mkdtemp(prefix='lda_google_map_'))
    (directory/'index.html').write_text(GOOGLE_REVIEW_COMPONENT_HTML,encoding='utf-8')
    return components.declare_component('lda_google_review',path=str(directory))


def result_summary_html(counts):
    order=[('GREEN','Compliant','#2dd4a6','✓'),('RED','Non-Compliant','#ff777e','!'),
           ('YELLOW','Needs Review','#f5c45c','?'),('GRAY','Unable to Locate','#a9b5c7','—')]
    total=sum(counts.get(code,0) for code,_,_,_ in order)
    cards=[]
    for code,label,color,icon in order:
        count=counts.get(code,0);percent=count/total*100 if total else 0
        cards.append(f'<div class="lda-result-card" style="--status-color:{color}">'
            f'<div class="lda-card-heading"><span>{label}</span><span class="lda-card-icon">{icon}</span></div>'
            f'<div class="lda-card-count">{count:,}</div>'
            f'<div class="lda-card-foot">{percent:.1f}% of placements</div></div>')
    return ("""<style>
.lda-result-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:18px 0 24px}
.lda-result-card{position:relative;overflow:hidden;background:linear-gradient(135deg,#182638,#101b29);border:1px solid #334155;border-top:4px solid var(--status-color);border-radius:14px;padding:20px 22px;box-shadow:0 5px 18px #0002;color:#f0f5fa}
.lda-card-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:15px;font-weight:600;color:var(--status-color)}
.lda-card-icon{display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--status-color);border-radius:50%;width:27px;height:27px;flex-shrink:0;font-size:17px}
.lda-card-count{font-size:42px;font-weight:750;line-height:1.2;letter-spacing:-1px;margin:14px 0 7px;font-variant-numeric:tabular-nums}
.lda-card-foot{font-size:12px;color:#b4c1d1}
@media(max-width:1000px){.lda-result-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:480px){.lda-result-grid{grid-template-columns:1fr}}
</style><div class="lda-result-grid">"""+''.join(cards)+'</div>')


def flagged_facilities_table(facility_hits):
    """One importable pin per mapped facility, retaining linked Screening rows."""
    columns=['Business name','Latitude','Longitude','Status','Category','OSM ID',
             'Nearest distance (ft)','Matched placement count','Screening worksheet rows',
             'Source grid rows','Location note','Applied distance(s) (ft)']
    grouped={}
    for hit in facility_hits:
        lat=hit['latitude'];lon=hit['longitude']
        if not (math.isfinite(lat) and math.isfinite(lon)):continue
        osm_id=str(hit.get('osm_id','')).strip()
        # Same OSM object across overlapping state extracts is a single facility.
        identity=('osm',osm_id) if osm_id else ('location',round(lat,7),round(lon,7),hit['name'],hit['category'])
        if identity not in grouped:
            grouped[identity]={'Business name':hit['name'] or 'Unnamed mapped facility',
                'Latitude':lat,'Longitude':lon,'Status':hit['status'],'Category':hit['category'],
                'OSM ID':osm_id,'Nearest distance (ft)':hit['distance_ft'],
                '_rows':set(),'_sources':set(),'_rules':set(),
                'Location note':'Mapped OSM location; street address not verified. Measure manually.'}
        record=grouped[identity]
        if hit['status']=='RED':record['Status']='RED'
        record['Nearest distance (ft)']=min(record['Nearest distance (ft)'],hit['distance_ft'])
        if hit.get('applied_distance_ft') is not None:record['_rules'].add(hit['applied_distance_ft'])
        record['_rows'].add(hit['screening_row'])
        record['_sources'].add(f"{hit['source_file']}: row {hit['source_row']}")
    records=[]
    for record in grouped.values():
        rows=record.pop('_rows');sources=record.pop('_sources');rules=record.pop('_rules')
        record['Applied distance(s) (ft)']=', '.join(map(str,sorted(rules)))
        record['Matched placement count']=len(rows)
        record['Screening worksheet rows']=', '.join(map(str,sorted(rows)))
        record['Source grid rows']='; '.join(sorted(sources))
        record['Status']=display_status(record['Status'])
        records.append(record)
    return pd.DataFrame(records,columns=columns)


def excel_bytes(result, facility_hits=None):
    out=io.BytesIO()
    with pd.ExcelWriter(out,engine='openpyxl') as writer:
        export=result.copy()
        export['screening_status']=export['screening_status'].map(display_status)
        export.to_excel(writer,index=False,sheet_name='Screening')
        ws=writer.sheets['Screening'];idx=result.columns.get_loc('screening_status')+1
        colors={'RED':'FCE8E8','YELLOW':'FFF0C2','GREEN':'E2F3E8','GRAY':'E6EAF0'}
        for row in range(2,len(result)+2):
            key=status_code(ws.cell(row,idx).value)
            for cell in ws[row]:cell.fill=PatternFill('solid',fgColor=colors.get(key,'FFFFFF'))
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='23384B')
        facilities=flagged_facilities_table(facility_hits or [])
        facilities.to_excel(writer,index=False,sheet_name='Flagged facilities')
        ws=writer.sheets['Flagged facilities']
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='23384B')
        for row in ws.iter_rows(min_row=2):
            color=colors.get(status_code(row[3].value),'FFFFFF')
            for cell in row:cell.fill=PatternFill('solid',fgColor=color)
            row[1].number_format='0.0000000';row[2].number_format='0.0000000'
        widths={'A':40,'B':17,'C':17,'D':12,'E':28,'F':24,'G':23,'H':25,'I':30,'J':50,'K':65,'L':28}
        for column,width in widths.items():ws.column_dimensions[column].width=width
    return out.getvalue()

def add_suggested_markets_and_rerun(codes):
    distances = {**st.session_state['stage6_settings']['distances']}
    for code in codes:
        distances.setdefault(code, 500)
    selection = [label for label, code in labels.items() if code in distances]
    st.session_state['stage6_dma_selection'] = selection
    st.session_state['stage6_dma_distances'].update(distances)
    st.session_state['stage6_settings'] = {'labels': selection, 'distances': distances}
    # Remount the picker so its browser state reflects the updated selection.
    st.session_state['stage6_panel_revision'] = st.session_state.get('stage6_panel_revision', 0) + 1
    st.session_state['stage6_rerun_requested'] = True


run_clicked=st.button('Run Nationwide Screening',type='primary')
run_clicked=st.session_state.pop('stage6_rerun_requested',False) or run_clicked
progress=st.status('Screening in progress… Checking placement coordinates',state='running',expanded=True) if run_clicked else None
try:
    chosen=[labels[x] for x in selected_labels]
    points=[]; point_rules=[];all_needed=set();valid=[]
    outside_points=[]
    for _,r in df.iterrows():
        rule=None
        try:
            lat=float(r[lat_col]);lon=float(r[lon_col]);
            if not (math.isfinite(lat) and math.isfinite(lon) and -90<=lat<=90 and -180<=lon<=180):raise ValueError('Invalid coordinate')
            rule=placement_dma_rule(lon,lat,chosen,dma_geoms,dma_distances,dma_names)
            needs=needed_states(lon,lat)
            if not needs:raise ValueError('No matching state geometry / unsupported territory')
            points.append((lat,lon,needs,''));all_needed.update(needs);valid.append((lat,lon))
        except OutsideSelectedDMA as exc:
            points.append((None,None,[],str(exc)))
            if run_clicked:
                outside_points.append(Point(lon,lat))
        except Exception as exc:points.append((None,None,[],str(exc)))
        point_rules.append(rule)

    if progress:progress.write(f'Coordinate checks complete: {len(valid):,} of {len(df):,} placements located in the selected DMAs.')
    available={};missing=[]
    for slug in sorted(all_needed):
        release=latest_asset(rels,f'stage6-osm-{slug}-','facilities.geojsonseq.gz')
        if release:available[slug]=release
        else:missing.append(slug)
    st.caption(f'Placements: {len(df):,} · Required state datasets: {", ".join(state_names[s] for s in sorted(all_needed)) or "none"}')
    if missing:st.error('Missing state snapshots: '+', '.join(state_names[s] for s in missing)+'. Placements requiring these states will be marked Unable to Locate.')

    fingerprint=(inventory_key,idx_id,tuple(sorted(dma_distances.items())),tuple(sorted((k,v['tag']) for k,v in available.items())))
    if run_clicked:
        start=time.perf_counter(); datasets={};load_errors={}
        suggested_additions=Counter()
        if outside_points:
            progress.update(label='Looking up markets for out-of-market placements…',state='running')
            lookup_codes,dma_tree=nationwide_dma_lookup(REPO,idx_id,TOKEN)
            _,matches=dma_tree.query(outside_points,predicate='covered_by')
            suggested_additions.update(lookup_codes[int(i)] for i in matches
                                       if lookup_codes[int(i)] not in dma_distances)
        # Bound the indexed area to actual inventory plus a conservative ~2km envelope.
        if valid:
            bounds=(min(p[1] for p in valid)-.04,min(p[0] for p in valid)-.04,
                    max(p[1] for p in valid)+.04,max(p[0] for p in valid)+.04)
            for slug,rel in available.items():
                progress.update(label=f'Loading {state_names[slug]} data…',state='running')
                try:
                    feats,tree,manifest=load_filtered_state(REPO,rel['file_id'],rel['manifest_id'],TOKEN,bounds)
                    date=datetime.fromisoformat(str(manifest['generated_utc']).replace('Z','+00:00'))
                    if (datetime.now(timezone.utc)-date).total_seconds()>10*86400:raise RuntimeError('Snapshot older than 10 days')
                    datasets[slug]=(feats,tree,manifest)
                    progress.write(f'{state_names[slug]} data ready.')
                except Exception as exc:
                    load_errors[slug]=str(exc)
                    progress.write(f'{state_names[slug]} data unavailable; affected placements will be marked Unable to Locate.')
        progress.update(label=f'Checking {len(points):,} placements…',state='running')
        results=[];facility_hits=[]
        for screening_row,(lat,lon,needs,error) in enumerate(points,start=2):
            checked=screening_row-1
            if checked==1 or checked%25==0 or checked==len(points):
                progress.update(label=f'Checking placements: {checked:,} of {len(points):,}',state='running')
            rec={'screening_status':'GRAY — Unable to screen','distance_ft':None,'nearby_facility':'',
                 'facility_category':'','osm_id':'','geometry_quality':'',
                 'facility_latitude':None,'facility_longitude':None,'screening_notes':'Human review required',
                 'state_snapshots':'','matched_dma':'','applied_distance_ft':None}
            rule=point_rules[screening_row-2]
            if rule:
                restriction=rule['distance']
                rec.update({'matched_dma':rule['dma'],'applied_distance_ft':restriction})
            if error:rec['screening_notes']=error;results.append(rec);continue
            bad=[s for s in needs if s not in datasets]
            if bad:
                rec['screening_notes']='Missing/stale/failed state coverage: '+', '.join(state_names[s] for s in bad)
                results.append(rec);continue
            project=local_projection(lon,lat); p=Point(0,0)
            radius_m=(restriction+50)*.3048;near=[]
            for slug in needs:
                feats,tree,_=datasets[slug]
                if tree is None:continue
                # Broad lon/lat envelope; exact final distance is measured in local meters.
                lat_delta=radius_m/110000*1.15
                lon_delta=radius_m/(111000*max(.1,math.cos(math.radians(lat))))*1.15
                search=box(lon-lon_delta,lat-lat_delta,lon+lon_delta,lat+lat_delta)
                for i in tree.query(search):
                    f=feats[int(i)]
                    try:d=p.distance(transform(project,f['geom']))
                    except Exception:continue
                    if d<=radius_m:near.append((d,f))
            near.sort(key=lambda item:item[0])
            red_trigger=next((item for item in near if facility_triggers_red(*item, restriction)),None)
            if red_trigger:status='RED — Potential conflict'
            elif near:status='YELLOW — Review required'
            else:status='GREEN — No detected conflict'
            trigger=red_trigger or (near[0] if near else None)
            rec['screening_status']=status
            rec['state_snapshots']='; '.join(f'{s}: {available[s]["tag"]}' for s in needs)
            rec['screening_notes']='OSM candidate screening only; human review required.'+(' '+rule['note'] if rule['note'] else '')
            if any(f['cats'] == ['university'] for _,f in near):
                rec['screening_notes']+=' Possible college or university — verify.'
            # Export every candidate within the review band, not only the primary
            # facility displayed in the placement row/pin popup.
            if status.startswith(('RED','YELLOW')):
                source=df.iloc[screening_row-2]
                for distance,facility in near:
                    facility_lat,facility_lon=facility_pin_location(facility['geom'])
                    if facility_lat is None or facility_lon is None:continue
                    hit_status='RED' if facility_triggers_red(distance,facility,restriction) else 'YELLOW'
                    facility_hits.append({'name':facility['name'],'latitude':facility_lat,
                        'longitude':facility_lon,'status':hit_status,'category':'; '.join(facility['cats']),
                        'osm_id':facility['osm_id'],'distance_ft':round(distance/.3048,1),
                        'screening_row':screening_row,'applied_distance_ft':restriction,'source_file':str(source[inventory_meta['file']]),
                        'source_row':str(source[inventory_meta['row']])})
            if trigger:
                d,f=trigger
                facility_lat,facility_lon=facility_pin_location(f['geom'])
                rec.update({'distance_ft':round(d/0.3048,1),'nearby_facility':f['name'],
                    'facility_category':'; '.join(f['cats']),'osm_id':f['osm_id'],'geometry_quality':f['quality'],
                    'facility_latitude':facility_lat,'facility_longitude':facility_lon})
            results.append(rec)
        progress.update(label='Preparing results and downloads…',state='running')
        st.session_state['stage6_result']=pd.concat([df.reset_index(drop=True),pd.DataFrame(results)],axis=1)
        st.session_state['stage6_fingerprint']=fingerprint
        st.session_state['stage6_elapsed']=time.perf_counter()-start
        st.session_state['stage6_errors']=load_errors
        st.session_state['stage6_facility_hits']=facility_hits
        st.session_state['stage6_suggested_additions']=suggested_additions
        progress.update(label='Screening complete',state='complete',expanded=False)
except Exception as exc:
    if progress:progress.update(label='Screening stopped — please review the error below',state='error',expanded=True)
    st.error(f'Unable to complete screening: {exc}')
    reset_control();st.stop()
if st.session_state.get('stage6_fingerprint')!=fingerprint:
    st.info('Run screening to generate results.');reset_control();st.stop()
res=st.session_state['stage6_result'];counts=Counter(str(x).split(' — ')[0] for x in res['screening_status'])
st.markdown(result_summary_html(counts),unsafe_allow_html=True)
suggested_additions=st.session_state.get('stage6_suggested_additions',{})
if suggested_additions:
    st.subheader('Suggested markets')
    st.caption('These markets contain coordinates for placements outside the selected DMAs. '
               'A placement on a shared boundary can count toward more than one market.')
    st.dataframe([{'Suggested DMA':dma_names[code],'Placements':count}
                  for code,count in sorted(suggested_additions.items(),key=lambda item:(-item[1],dma_names[item[0]]))],
                 hide_index=True)
    st.caption('New markets use 500 ft. Previously applied distances stay as set.')
    st.markdown('''<style>
    .st-key-stage6_suggested_rerun_action button {
        background-color: #ffd166;
        border-color: #ffd166;
        color: #111821;
    }
    .st-key-stage6_suggested_rerun_action button:hover {
        background-color: #ffe096;
        border-color: #ffe096;
        color: #111821;
    }
    .st-key-stage6_suggested_rerun_action button:focus-visible {
        outline: 2px solid #ffd166;
        outline-offset: 2px;
    }
    </style>''',unsafe_allow_html=True)
    with st.container(key='stage6_suggested_rerun_action'):
        st.button('Add suggested markets and rerun screening',
                  on_click=add_suggested_markets_and_rerun,args=(tuple(suggested_additions),))
if st.session_state['stage6_errors']:
    st.error('State data errors: '+ '; '.join(f'{state_names[k]}: {v}' for k,v in st.session_state['stage6_errors'].items()))
priority=[x for x in ('screening_status','distance_ft','nearby_facility','facility_category','matched_dma','applied_distance_ft','Unit #','Location Description','screening_notes') if x in res]
res_display=res.copy()
res_display['screening_status']=res_display['screening_status'].map(display_status)
a,b=st.columns(2)
with a:st.download_button('Download color-coded Excel',excel_bytes(res,st.session_state.get('stage6_facility_hits',[])),'ABI_Stage6_screening.xlsx')
with b:st.download_button('Download screening CSV',res_display.to_csv(index=False).encode('utf-8-sig'),'ABI_Stage6_screening.csv')
st.caption('Excel includes a Flagged facilities worksheet with all detected Non-Compliant / Needs Review candidates and importable coordinates. In My Maps, use Latitude/Longitude for location and Business name for the title.')
st.subheader('Review map')
st.caption('Circles mark placements. Pin-shaped markers identify flagged facilities, including colleges and universities that need separate review. Facility pins use mapped OSM locations, not verified street addresses.')
map_rows=[(lat,lon,r) for (lat,lon,_,_),(_,r) in zip(points,res.iterrows()) if lat is not None]
if map_rows:
    google_key=st.secrets.get('GOOGLE_MAPS_API_KEY','').strip()
    college_pins=additional_college_pins(map_rows,st.session_state.get('stage6_facility_hits',[]))
    osm_sites=[]
    if st.checkbox('Show OSM site pins',key='stage6_show_osm_sites'):
        with st.spinner('Loading OSM sites across the selected markets…'):
            osm_sites,unavailable=dma_osm_pin_records(map_rows)
        college_positions={(round(pin['position']['lat'],7),round(pin['position']['lng'],7))
                           for pin in college_pins}
        osm_sites=[site for site in osm_sites if
                   (round(site['position']['lat'],7),round(site['position']['lng'],7)) not in college_positions]
        if unavailable:
            st.warning('OSM site pins unavailable for: '+', '.join(unavailable))
        if google_key and osm_sites:
            st.caption('Zoom in to see the black OSM site pins. Colored pins identify flagged sites.')
        if not osm_sites and not unavailable:
            st.caption('No additional OSM sites found in the selected markets.')
    if google_key:
        st.caption('Google Maps business labels appear as you zoom in. Ruler and pin controls run within the map.')
        map_records=google_review_map_records(map_rows)
        map_identity=hashlib.sha256((json.dumps([map_records,osm_sites,college_pins],allow_nan=False)+google_key).encode()).hexdigest()[:20]
        google_review_component()(records=map_records,osm_sites=osm_sites,college_pins=college_pins,api_key=google_key,
                                  key='google_review_'+map_identity,default=None)
    else:
        st.info('Add GOOGLE_MAPS_API_KEY in Streamlit Secrets to use Google Maps. Showing the existing CARTO map meanwhile.')
        center=[sum(x[0] for x in map_rows)/len(map_rows),sum(x[1] for x in map_rows)/len(map_rows)]
        m=folium.Map(location=center,zoom_start=10,tiles=None,control_scale=True,height=600,width='100%')
        url=st.secrets.get('CARTO_TILE_URL','');key=st.secrets.get('CARTO_API_KEY','')
        if '{api_key}' in url:url=url.replace('{api_key}',key) if key else ''
        if url:folium.TileLayer(tiles=url,attr='CARTO / OpenStreetMap contributors',name='CARTO dark').add_to(m)
        else:folium.TileLayer('CartoDB dark_matter',name='CARTO dark (public tiles)').add_to(m)
        folium.TileLayer(
            tiles='https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='Esri, Vantor, Earthstar Geographics, and the GIS User Community',
            name='Satellite', show=False, max_native_zoom=19,
        ).add_to(m)
        LiveRuler().add_to(m)
        colors={'RED':'red','YELLOW':'orange','GREEN':'green','GRAY':'gray'}
        placement_layer=folium.FeatureGroup(name='Placements').add_to(m)
        facility_layer=folium.FeatureGroup(name='Flagged facilities').add_to(m)
        if osm_sites:
            osm_layer=MarkerCluster(name='OSM sites').add_to(m)
            for site in osm_sites:
                position=site['position']
                folium.Marker([position['lat'],position['lng']],
                              icon=folium.Icon(color='black',icon='exclamation-sign',prefix='glyphicon'),
                              popup=folium.Popup(site['popup'],max_width=350)).add_to(osm_layer)
        for row_number,(lat,lon,r) in enumerate(map_rows[:1000],start=1):
            flag=status_code(r['screening_status']);status=display_status(r['screening_status']);color=colors.get(flag,'gray')
            popup=f'<b>{html.escape(placement_pin_title(r,row_number))}</b><br>'+html.escape(status)+'<br>Distance: '+html.escape(str(r['distance_ft']))+' ft<br>Facility: '+html.escape(str(r['nearby_facility'])[:150])
            folium.CircleMarker([lat,lon],radius=6,color=color,fill=True,fill_opacity=.85,popup=folium.Popup(popup,max_width=350)).add_to(placement_layer)
            facility_lat=r.get('facility_latitude');facility_lon=r.get('facility_longitude')
            if flag in ('RED','YELLOW') and pd.notna(facility_lat) and pd.notna(facility_lon):
                name=str(r['nearby_facility']).strip() or 'Unnamed mapped facility'
                facility_popup=(f'<b>Flagged facility for placement {row_number}</b><br>'
                    +html.escape(name)+'<br>'+html.escape(str(r['facility_category']))
                    +'<br>OSM: '+html.escape(str(r['osm_id']))
                    +'<br>Pin marks mapped facility location; measure manually.')
                folium.Marker([float(facility_lat),float(facility_lon)],
                    icon=folium.Icon(color=color,icon='exclamation-sign',prefix='glyphicon'),
                    tooltip='Flagged facility: '+html.escape(name),
                    popup=folium.Popup(facility_popup,max_width=350)).add_to(facility_layer)
        for site in college_pins:
            position=site['position']
            folium.Marker([position['lat'],position['lng']],
                          icon=folium.Icon(color='orange',icon='exclamation-sign',prefix='glyphicon'),
                          tooltip=html.escape(site['title']),
                          popup=folium.Popup(site['popup'],max_width=350)).add_to(facility_layer)
        folium.LayerControl(collapsed=False).add_to(m)
        # Explicit heights on both Leaflet and its iframe prevent a collapsed map.
        # Pan, zoom, popups and measuring stay in the browser without app reruns.
        components.html(m.get_root().render(),height=620,scrolling=False)
        if len(map_rows)>1000:st.caption('Map displays first 1,000 placements; exports include all.')
    if len(map_rows)>1000 and google_key:
        st.caption('Map displays first 1,000 placements; exports include all.')
st.caption('Coverage: 50 US states + DC, subject to successful state builds. Territories are not yet supported. OSM incompleteness, missing footprints and facility classifications require manual review for ALL statuses.')

st.divider()
reset_control()
