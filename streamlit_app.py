"""ABI OOH public-data geographic coverage prototype. No inventory ingestion."""
import io
import os
import uuid
from pathlib import Path
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests
import streamlit as st
from pyproj import CRS, Transformer
from shapely.geometry import Point, Polygon, shape
from shapely.ops import transform, unary_union
from shapely.prepared import prep

DMA_URL = ('https://servicesdev.arcgis.com/f126c8da131543019b05e4bfab6fc6ac/'
           'ArcGIS/rest/services/Esri_Demographics_2025_Base_01_gdb/FeatureServer/2/query')
OVERPASS = ['https://overpass.kumi.systems/api/interpreter', 'https://overpass-api.de/api/interpreter']
CATEGORY_KEYS = {
    'amenity': {'school', 'kindergarten', 'childcare', 'place_of_worship'},
    'building': {'school', 'kindergarten'},
    'landuse': {'education'},
    'healthcare': {'rehabilitation', 'addiction'},
    'healthcare:speciality': {'addiction'},
    'social_facility': {'addiction', 'drug_addiction', 'substance_abuse', 'rehabilitation'},
}

st.set_page_config(page_title='ABI OOH · LDA Coverage Generator', page_icon='🗺️', layout='wide')
st.caption("ABI OOH · GEOGRAPHIC COVERAGE")
st.title("LDA Restricted-Area Generator")
st.write("Public OpenStreetMap data only. No proprietary placement uploads, no accounts, no Google Drive access.")

@st.cache_data(ttl=86400, show_spinner=False)
def load_markets():
    features=[]; offset=0; seen=set()
    with requests.Session() as session:
        while True:
            resp=session.get(DMA_URL, params={'where':'1=1','outFields':'ID,name','returnGeometry':'true',
                'outSR':'4326','orderByFields':'OBJECTID ASC','resultOffset':offset,
                'resultRecordCount':40,'f':'geojson'}, timeout=90)
            resp.raise_for_status(); payload=resp.json()
            if 'error' in payload: raise RuntimeError(str(payload['error']))
            batch=payload.get('features',[])
            if not batch: break
            for f in batch:
                key=str(f.get('properties',{}).get('ID','')).strip()
                if not key or key in seen: raise RuntimeError('Duplicate or missing DMA ID; refusing incomplete data')
                seen.add(key); features.append(f)
            offset+=len(batch)
            if len(features)>300: raise RuntimeError('Unexpected DMA record count')
    if len(features)!=209: raise RuntimeError(f'Expected 209 DMAs, received {len(features)}. Try again later.')
    return features

def label(f):
    p=f['properties']; return f"{p.get('name','Unnamed')} [{p.get('ID','?')}]"

def classify(tags):
    def v(k):return str(tags.get(k,'')).strip().lower()
    blob=' '.join(str(x).lower() for x in tags.values())
    if (v('healthcare:speciality')=='addiction' or v('healthcare')=='addiction'
        or v('social_facility') in {'addiction','drug_addiction','substance_abuse'}
        or any(term in blob for term in ['substance abuse','drug addiction','opioid treatment','alcohol treatment','detox','chemical dependency'])):
        return 'Substance use'
    if v('amenity')=='place_of_worship':return 'Place of worship'
    if v('amenity') in ('childcare','kindergarten') or v('building')=='kindergarten':return 'Childcare'
    if v('amenity')=='school' or v('building')=='school' or v('landuse')=='education':return 'School'
    return None

def overpass_query(bounds):
    west,south,east,north=bounds
    bb=f'{south:.6f},{west:.6f},{north:.6f},{east:.6f}'
    parts=[]
    for key,values in CATEGORY_KEYS.items():
        for value in sorted(values):
            parts.append(f'nwr["{key}"="{value}"]({bb});')
    return '[out:json][timeout:150];('+''.join(parts)+');out geom;'

def get_osm(bounds):
    query=overpass_query(bounds)
    errors=[]
    for endpoint in OVERPASS:
        try:
            response=requests.post(endpoint, data={'data':query}, timeout=185,
                headers={'User-Agent':'ABI-OOH-LDA-coverage-prototype/0.1'})
            response.raise_for_status()
            data=response.json()
            if 'elements' not in data: raise RuntimeError('Incomplete Overpass response')
            return data['elements']
        except Exception as exc:
            errors.append(f'{endpoint}: {type(exc).__name__}: {exc}')
    raise RuntimeError('Both public Overpass services failed. No KMZ was generated. '+ ' | '.join(errors))

def element_geom(element):
    if element['type']=='node':return Point(element['lon'],element['lat'])
    coords=[(p['lon'],p['lat']) for p in element.get('geometry',[]) if 'lon' in p and 'lat' in p]
    if len(coords)<2:return None
    if len(coords)>=4 and coords[0]==coords[-1]:
        polygon=Polygon(coords)
        if polygon.is_valid and not polygon.is_empty:return polygon
    # A line or incomplete relation is not safely modeled as an area; use a point proxy.
    return Point(coords[len(coords)//2])

def kmz_bytes(name, dma_id, dma_geom, buffers, feet, source_count):
    def coordstr(ring):
        return ' '.join(f'{x:.7f},{y:.7f},0' for x,y in ring.coords)
    def polygons(g):
        if g.geom_type=='Polygon':return [g]
        if g.geom_type=='MultiPolygon':return list(g.geoms)
        if g.geom_type=='GeometryCollection':return [p for x in g.geoms for p in polygons(x)]
        return []
    def placemark(poly, title, style):
        rings=f'<outerBoundaryIs><LinearRing><coordinates>{coordstr(poly.exterior)}</coordinates></LinearRing></outerBoundaryIs>'
        for hole in poly.interiors:
            rings+=f'<innerBoundaryIs><LinearRing><coordinates>{coordstr(hole)}</coordinates></LinearRing></innerBoundaryIs>'
        return f'<Placemark><name>{escape(title)}</name><styleUrl>#{style}</styleUrl><Polygon>{rings}</Polygon></Placemark>'
    header=('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f'<name>{escape(name)} — {feet} ft restricted coverage</name>'
            '<Style id="red"><LineStyle><color>ff2020e8</color><width>1</width></LineStyle><PolyStyle><color>7f2020e8</color></PolyStyle></Style>'
            '<Style id="border"><LineStyle><color>ff7ed6e8</color><width>2</width></LineStyle><PolyStyle><fill>0</fill></PolyStyle></Style>')
    parts=[header,'<Folder><name>DMA boundary</name>']
    parts += [placemark(p,'DMA boundary','border') for p in polygons(dma_geom)]
    parts.append('</Folder><Folder><name>Potential restricted areas</name>')
    for i,(poly,category) in enumerate(buffers):
        for piece in polygons(poly):parts.append(placemark(piece,f'{category} #{i+1}','red'))
    parts.append('</Folder></Document></kml>')
    content=''.join(parts).encode('utf-8')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        z.writestr('doc.kml',content)
        z.writestr('SOURCE_NOTES.txt',f'''ABI OOH prototype | {name} [{dma_id}]\nBuffer: {feet} feet\nOSM features included: {source_count}\nGenerated UTC: {datetime.now(timezone.utc).isoformat()}\nSource: OpenStreetMap via public Overpass servers.\nWARNING: OSM is incomplete. Feature tagging, geometry and coverage may be inaccurate.\nPoint approximations are used for some incomplete line/relation geometries.\nThis output is for preliminary review only; human/legal verification required.\n''')
    return out.getvalue()

def generate(feature,feet,progress):
    name=feature['properties']['name']; dma_id=str(feature['properties']['ID'])
    dma=shape(feature['geometry'])
    lon,lat=dma.centroid.x,dma.centroid.y
    zone=max(1,min(60,int((lon+180)//6)+1))
    epsg=(32600 if lat>=0 else 32700)+zone
    to_m=Transformer.from_crs('EPSG:4326',f'EPSG:{epsg}',always_xy=True).transform
    to_ll=Transformer.from_crs(f'EPSG:{epsg}','EPSG:4326',always_xy=True).transform
    dma_m=transform(to_m,dma); margin=feet*0.3048
    search_area=dma_m.buffer(margin)
    bbox=transform(to_ll,search_area).bounds
    progress.info('Requesting mapped restricted locations from OpenStreetMap…')
    elements=get_osm(bbox)
    if not elements:raise RuntimeError('No OSM features returned; refusing to create a possibly misleading empty layer.')
    progress.info(f'Filtering {len(elements):,} OSM features against the DMA…')
    clipped=prep(search_area); buffers=[]; seen=set(); count=0
    for e in elements:
        key=(e.get('type'),e.get('id'))
        if key in seen:continue
        seen.add(key)
        category=classify(e.get('tags',{}))
        if not category:continue
        g=element_geom(e)
        if g is None or g.is_empty:continue
        gm=transform(to_m,g)
        if not clipped.intersects(gm):continue
        count+=1
        buffered=gm.buffer(margin,resolution=8).intersection(dma_m)
        if buffered.is_empty:continue
        # Light simplification to keep file sizes manageable; no topology-breaking simplification.
        buffered=buffered.simplify(1.0,preserve_topology=True)
        buffers.append((transform(to_ll,buffered),category))
    if not buffers:raise RuntimeError('No restricted-area features retained; refusing an empty compliance layer.')
    progress.info(f'Packaging {len(buffers):,} restricted areas into Google Earth KMZ…')
    return kmz_bytes(name,dma_id,dma,buffers,feet,count),count

st.caption("Web prototype v3 · Public geographic data only")
st.info("Select **Load DMA markets** once. The 209 market boundaries are cached for subsequent sessions.")
if "markets" not in st.session_state:
    if st.button("Load DMA markets", type="primary"):
        try:
            with st.spinner("Fetching DMA boundaries…"):
                st.session_state["markets"] = load_markets()
        except Exception as exc:
            st.error(f"DMA reference data could not be loaded: {type(exc).__name__}: {exc}")
    if "markets" not in st.session_state:
        st.stop()

markets = sorted(st.session_state["markets"], key=lambda f: (str(f["properties"].get("name","")).casefold(), str(f["properties"].get("ID",""))))
st.caption(f"{len(markets)} DMA markets available · Alphabetically sorted")
st.divider()
left, right = st.columns([3,1])
with left:
    chosen = st.multiselect("DMA markets (select up to 5)", markets, format_func=label,
        max_selections=5, placeholder="Search DMA markets alphabetically…")
with right:
    feet = st.selectbox("Restricted-area distance (feet)", [500,250], index=0)
st.caption("Each DMA is processed separately. For multiple selections, the download is a ZIP containing one KMZ per DMA. Large markets may take several minutes each.")

def safe_filename(name):
    return re.sub(r"[^a-zA-Z0-9_-]+","_",name).strip("_")[:75] or "DMA"

def store_recovery_file(payload, filename):
    """Temporary server-side recovery link; available until Streamlit restarts/cleans storage."""
    directory = Path(__file__).resolve().parent / "static"
    directory.mkdir(parents=True, exist_ok=True)
    # Do not retain unlimited temporary results.
    import time as _time
    for old in directory.glob("abi_*"):
        try:
            if _time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    token = uuid.uuid4().hex[:16]
    path = directory / f"abi_{token}_{filename}"
    tmp = directory / f".{path.name}.tmp"
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    return path.name

if st.button("🗺️ Generate restricted-area files", type="primary", disabled=not chosen):
    st.session_state.pop("result", None)
    with st.status("Generating selected markets…", expanded=True) as status:
        try:
            start = time.monotonic()
            outputs = []
            for i, feature in enumerate(chosen, 1):
                name = feature["properties"]["name"]
                st.write(f"Market {i}/{len(chosen)}: {name}")
                content, count = generate(feature, feet, st)
                filename = f"{safe_filename(name)}_{feature['properties']['ID']}_{feet}ft_REVIEW_ONLY.kmz"
                outputs.append((filename, content, count, name))
                st.write(f"Completed {name}: {count:,} mapped features")
            if len(outputs) == 1:
                filename, payload = outputs[0][0], outputs[0][1]
                mime = "application/vnd.google-earth.kmz"
            else:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=2) as archive:
                    for filename_i, content_i, _, _ in outputs:
                        archive.writestr(filename_i, content_i)
                    archive.writestr("README.txt", "Each KMZ is a separate DMA coverage layer. Load each KMZ into Google Earth. These are preliminary OpenStreetMap screening layers, NOT verified legal clearance.\\n")
                payload = buf.getvalue()
                filename = f"ABI_OOH_{len(outputs)}_DMAs_{feet}ft_REVIEW_ONLY.zip"
                mime = "application/zip"
            recovery = store_recovery_file(payload, filename)
            st.session_state["result"] = {
                "payload": payload, "filename": filename, "mime": mime,
                "recovery": recovery, "markets": len(outputs),
                "count": sum(o[2] for o in outputs),
                "elapsed": time.monotonic()-start
            }
            status.update(label="Files generated successfully", state="complete", expanded=False)
        except Exception as exc:
            status.update(label="Generation failed — no complete download", state="error", expanded=True)
            st.error(f"{type(exc).__name__}: {exc}")

if "result" in st.session_state:
    result = st.session_state["result"]
    st.success(f"{result['markets']} DMA(s), {result['count']:,} source features, {result['elapsed']:,.1f} seconds.")
    # Plain static URL is a fallback if the Streamlit download-button JS chunk fails.
    st.markdown(f"**Direct recovery download:** [Download {result['filename']}](./app/static/{result['recovery']})")
    st.caption("Recovery links are temporary, public, and may disappear after app restart or redeployment. Save your file locally.")
    st.download_button("⬇️ Download file", data=result["payload"], file_name=result["filename"],
        mime=result["mime"], type="primary", on_click="ignore")

st.warning("Preliminary screening only. OpenStreetMap may be incomplete or inaccurate. A missing red area does not establish legal clearance. Public Overpass servers may time out or reject large DMA queries. No proprietary grids are accepted or stored.")
