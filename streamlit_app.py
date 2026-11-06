"""ABI OOH LDA Quick Check v5 beta. Geometry-first screening; no authentication."""
import io
import math
import re
import zipfile
from datetime import datetime, timezone

import pandas as pd
import folium
from folium import plugins
from streamlit_folium import st_folium
from branca.element import MacroElement
from jinja2 import Template
import requests
import streamlit as st
from pyproj import CRS, Transformer
from shapely.geometry import Point, Polygon, shape, mapping, MultiPolygon, LineString
from shapely.ops import transform, unary_union

st.set_page_config(page_title="ABI OOH · LDA Quick Check", page_icon="📍", layout="wide")
st.markdown("""<style>.block-container{padding-top:1.7rem;max-width:1500px}h1{letter-spacing:-.035em}div[data-testid="stMetric"]{background:#152334;padding:14px;border-radius:12px}</style>""", unsafe_allow_html=True)
OSM_ENDPOINTS = ["https://overpass.kumi.systems/api/interpreter", "https://overpass-api.de/api/interpreter"]
CATEGORIES = {
    "amenity": ["school", "kindergarten", "childcare", "place_of_worship", "college", "university"],
    "building": ["school", "kindergarten", "college", "university"],
    "healthcare": ["rehabilitation", "addiction"],
    "healthcare:speciality": ["addiction"],
    "social_facility": ["addiction", "drug_addiction", "substance_abuse", "rehabilitation"],
}
STATUS_COLORS = {"Potential conflict": [235, 71, 83, 220], "Needs more screening": [245, 190, 56, 220], "University approval": [245, 190, 56, 220], "No mapped hit": [61, 192, 143, 220]}


def classify(tags):
    def val(k): return str(tags.get(k, "")).lower().strip()
    if val("amenity") in ("college", "university") or val("building") in ("college", "university"):
        return "University approval"
    if val("healthcare:speciality") == "addiction" or val("healthcare") == "addiction" or val("social_facility") in ("addiction", "drug_addiction", "substance_abuse", "rehabilitation"):
        return "Substance use"
    if val("amenity") == "place_of_worship": return "Place of worship"
    if val("amenity") in ("childcare", "kindergarten") or val("building") == "kindergarten": return "Childcare"
    if val("amenity") == "school" or val("building") == "school": return "School"
    return None


def osm_geometry(e):
    """Use mapped footprints; retain incomplete ways as point-only *uncertain* sources.

    Overpass `out geom` relations may include member geometries rather than top-level
    coordinates. We conservatively union closed outer rings; holes are not legal-grade.
    """
    if e.get("type") == "node":
        return Point(float(e["lon"]), float(e["lat"])), "point"
    rings = []
    points = [(p["lon"], p["lat"]) for p in e.get("geometry", []) if "lon" in p and "lat" in p]
    if len(points) >= 4 and points[0] == points[-1]:
        rings.append(points)
    if e.get("type") == "relation":
        for member in e.get("members", []):
            if member.get("role") not in ("outer", ""):
                continue
            pts = [(p["lon"], p["lat"]) for p in member.get("geometry", []) if "lon" in p and "lat" in p]
            if len(pts) >= 4 and pts[0] == pts[-1]:
                rings.append(pts)
    polys=[]
    for ring in rings:
        poly=Polygon(ring)
        if not poly.is_valid: poly=poly.buffer(0)
        if not poly.is_empty and poly.area>0: polys.append(poly)
    if polys:
        geom=unary_union(polys)
        return geom, "footprint"
    # An incomplete way or relation is NOT a reliable footprint.
    center=e.get("center") or {}
    if "lat" in center and "lon" in center:
        return Point(float(center["lon"]),float(center["lat"])), "incomplete"
    if points:
        return Point(float(points[0][0]),float(points[0][1])), "incomplete"
    return None, "missing"


class LiveRuler(MacroElement):
    """Two-click Leaflet ruler with continuously updating feet on mouse movement."""
    _template = Template("""
    {% macro script(this, kwargs) %}
    (function(){
      var map={{this._parent.get_name()}};
      var active=false, start=null, line=null, tip=null;
      var ctl=L.control({position:'topleft'});
      ctl.onAdd=function(){
        var btn=L.DomUtil.create('button','leaflet-bar');
        btn.type='button';btn.innerHTML='📏';btn.title='Ruler: click start, move, click finish; press Esc to cancel';
        btn.style.cssText='background:#172b3c;color:white;border:1px solid #94a3b8;width:34px;height:34px;font-size:19px;cursor:pointer';
        L.DomEvent.disableClickPropagation(btn);
        L.DomEvent.on(btn,'click',function(){active=!active;start=null;if(line){map.removeLayer(line);line=null;}if(tip){map.removeLayer(tip);tip=null;}btn.style.background=active?'#0e7490':'#172b3c';});
        return btn;
      };ctl.addTo(map);
      map.on('click',function(e){
        if(!active)return;
        if(!start){start=e.latlng;line=L.polyline([start,start],{color:'#38bdf8',weight:3,dashArray:'7 5'}).addTo(map);}
        else {line.setLatLngs([start,e.latlng]);active=false;start=null;}
      });
      map.on('mousemove',function(e){
        if(!active||!start)return;
        var ft=start.distanceTo(e.latlng)*3.280839895;
        line.setLatLngs([start,e.latlng]);
        if(tip)map.removeLayer(tip);
        tip=L.tooltip({permanent:true,direction:'top',className:'distance-tip'}).setLatLng(e.latlng).setContent(ft.toLocaleString(undefined,{maximumFractionDigits:1})+' ft').addTo(map);
      });
      map.on('keydown',function(e){if(e.originalEvent&&e.originalEvent.key==='Escape'){active=false;start=null;if(line)map.removeLayer(line);if(tip)map.removeLayer(tip);}});
    })();
    {% endmacro %}
    """)


def overpass_query(bbox):
    s, w, n, e = bbox
    selector = "".join(f'nwr["{k}"="{v}"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});' for k, vals in CATEGORIES.items() for v in vals)
    return "[out:json][timeout:150];(" + selector + ");out center geom;"


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_osm(bbox):
    errors = []
    for endpoint in OSM_ENDPOINTS:
        try:
            response = requests.post(endpoint, data={"data": overpass_query(bbox)}, timeout=190,
                                     headers={"User-Agent": "ABI-OOH-QuickCheck-beta/0.1"})
            response.raise_for_status()
            result = response.json()
            if "elements" not in result: raise ValueError("No elements key returned")
            return result["elements"]
        except Exception as exc:
            errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
    raise RuntimeError("OSM retrieval failed; no screening results were produced. " + " | ".join(errors))


def identify_column(cols, names):
    norm = {re.sub(r"[^a-z0-9]", "", str(c).lower()): c for c in cols}
    for name in names:
        if re.sub(r"[^a-z0-9]", "", name.lower()) in norm:
            return norm[re.sub(r"[^a-z0-9]", "", name.lower())]
    return None


def screen(df, lat_col, lon_col, id_col, feet):
    # Explicitly prevent common financial columns from being retained or exported.
    prohibited = re.compile(r"(^|[^a-z])(rate|cpm|cost|budget|discount|net price|gross price|spend|investment)([^a-z]|$)",re.I)
    records = df.drop(columns=[c for c in df.columns if prohibited.search(str(c))]).copy().reset_index(drop=True)
    for required in (lat_col,lon_col,id_col):
        if required not in records.columns:
            raise ValueError(f"Required column {required!r} was classified as financial. Rename the location column and retry.")
    records["Latitude"] = pd.to_numeric(records[lat_col], errors="coerce")
    records["Longitude"] = pd.to_numeric(records[lon_col], errors="coerce")
    valid = records["Latitude"].between(-90, 90) & records["Longitude"].between(-180, 180)
    if not valid.all():
        raise ValueError(f"{(~valid).sum()} rows have invalid coordinates. Correct them before screening; no rows were silently dropped.")
    if records.empty: raise ValueError("The uploaded file contains no placements.")
    if len(records) > 3000: raise ValueError("Beta limit: 3,000 placements per screening run.")
    if records[id_col].isna().any(): raise ValueError("Placement IDs cannot be blank.")
    records["Placement ID"] = records[id_col].astype(str)
    # Beta limitation: a single local UTM zone; restrict area to prevent misleading distances.
    center_lat = float(records["Latitude"].median()); center_lon = float(records["Longitude"].median())
    zone = max(1, min(60, int((center_lon + 180) // 6) + 1))
    epsg = (32600 if center_lat >= 0 else 32700) + zone
    forward = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
    reverse = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True).transform
    xy = [transform(forward, Point(lon, lat)) for lat, lon in zip(records["Latitude"], records["Longitude"])]
    minx = min(p.x for p in xy); maxx = max(p.x for p in xy)
    miny = min(p.y for p in xy); maxy = max(p.y for p in xy)
    if max(maxx-minx, maxy-miny) > 18000:
        raise ValueError("This beta supports placements within an approximately 18 km square. Split large markets into smaller batches. No results generated.")
    margin = feet * 0.3048 + 50 * 0.3048 + 200
    llbounds = transform(reverse, Polygon([(minx-margin,miny-margin),(maxx+margin,miny-margin),(maxx+margin,maxy+margin),(minx-margin,maxy+margin)])).bounds
    bbox = (llbounds[1],llbounds[0],llbounds[3],llbounds[2])
    elements = fetch_osm(tuple(round(x, 7) for x in bbox))
    if not elements: raise ValueError("OSM returned zero facilities. Refusing to classify placements as green.")
    facilities = []
    seen = set()
    for e in elements:
        key = (e.get("type"), e.get("id"))
        if key in seen: continue
        seen.add(key)
        cat = classify(e.get("tags", {}))
        if not cat: continue
        g, quality = osm_geometry(e)
        if g is None: continue
        gm = transform(forward, g)
        if gm.is_empty: continue
        pt = g.representative_point()
        facilities.append({"key": f"{key[0]}/{key[1]}", "name": e.get("tags", {}).get("name", "Unnamed OSM facility"), "category": cat,
                           "geometry": gm, "geometry_quality": quality, "lat": pt.y, "lon": pt.x})
    if not facilities: raise ValueError("No usable facility geometries returned. Refusing to classify placements as green.")
    # Coverage uncertainty: point/incomplete geometries are never treated as
    # authoritative red polygons. They can only trigger additional review.
    rows=[]; matched={}
    match_geometry={}
    for f in facilities:
        if f["geometry_quality"] == "footprint":
            match_geometry[f["key"]] = f["geometry"]

    for i, point in enumerate(xy):
        nearby=[]
        for f in facilities:
            distance = point.distance(f["geometry"]) / 0.3048
            if distance <= feet + 50:
                nearby.append((distance, f))
        nearby.sort(key=lambda x:x[0])
        red = [(d,f) for d,f in nearby if d <= feet and f["category"] != "University approval" and f["geometry_quality"] == "footprint"]
        uni = [(d,f) for d,f in nearby if d <= feet and f["category"] == "University approval" and f["geometry_quality"] == "footprint"]
        band = [(d,f) for d,f in nearby if feet < d <= feet+50 or (d <= feet and f["geometry_quality"] != "footprint")]
        if red: status="Potential conflict"; relevant=red
        elif uni: status="University approval"; relevant=uni
        elif band: status="Needs more screening"; relevant=band
        else: status="No mapped hit"; relevant=[]
        closest = relevant[0] if relevant else (nearby[0] if nearby else None)
        rows.append({"Screening Status":status, "Matched Facility":closest[1]["name"] if closest else "",
                     "Facility Category":closest[1]["category"] if closest else "",
                     "Distance to Facility (ft)":round(closest[0],1) if closest else None,
                     "Matched OSM ID":closest[1]["key"] if closest else "",
                     "Source Geometry":closest[1]["geometry_quality"] if closest else "",
                     "Recommendation":"Pending", "Reviewer Notes":""})
        for distance, f in relevant:
            matched.setdefault(f["key"], {"Facility Name":f["name"], "Facility Category":f["category"], "OSM ID":f["key"], "Geometry Quality":f["geometry_quality"],
                                         "Latitude":f["lat"], "Longitude":f["lon"], "Affected Placement IDs":set()})
            matched[f["key"]]["Affected Placement IDs"].add(records.loc[i,"Placement ID"])
    output = pd.concat([records, pd.DataFrame(rows)],axis=1)
    facility_rows=[]
    for f in matched.values():
        f["Affected Placement IDs"]="; ".join(sorted(f["Affected Placement IDs"]))
        facility_rows.append(f)
    return output, pd.DataFrame(facility_rows, columns=["Facility Name","Facility Category","OSM ID","Geometry Quality","Latitude","Longitude","Affected Placement IDs"]), len(facilities), facilities, epsg


def csv_bytes(df): return df.to_csv(index=False).encode("utf-8-sig")

st.caption("ABI OOH  /  LDA COMPLIANCE HUB  /  BETA v5")
st.title("Quick Check · Campaign Screening")
st.write("Screen sanitized placement coordinates, investigate mapped building-footprint buffers, measure distances, and prepare a Google My Maps recommendation export.")
st.warning("**Beta / preliminary only.** OSM is incomplete, and some facilities are only mapped as points or may be missing entirely; green means no intersection with the retrieved mapped facilities, **not legal clearance**. University approval and the 50-foot review band are separate review categories. No authentication: use synthetic or explicitly approved data only.")

with st.expander("1 · Upload sanitized placement inventory", expanded=True):
    uploaded = st.file_uploader("CSV or Excel (no rates, budgets, client financial information)", type=["csv","xlsx"])
    if uploaded:
        try:
            if uploaded.name.lower().endswith(".csv"):
                data=pd.read_csv(uploaded)
            else:
                data=pd.read_excel(uploaded)
            if data.empty: raise ValueError("Empty file")
            cols=list(data.columns)
            c1,c2,c3=st.columns(3)
            lat_guess=identify_column(cols,["Latitude","Lat","Site Latitude"])
            lon_guess=identify_column(cols,["Longitude","Lon","Long","Lng","Site Longitude"])
            id_guess=identify_column(cols,["Placement ID","Unit ID","Spot ID","ID","Media ID"])
            lat_col=c1.selectbox("Latitude column",cols,index=cols.index(lat_guess) if lat_guess in cols else 0)
            lon_col=c2.selectbox("Longitude column",cols,index=cols.index(lon_guess) if lon_guess in cols else min(1,len(cols)-1))
            id_col=c3.selectbox("Placement ID column",cols,index=cols.index(id_guess) if id_guess in cols else 0)
            feet=st.selectbox("Screening buffer",[500,250],index=0,format_func=lambda x:f"{x} feet + 50-foot review band")
            st.caption(f"{len(data):,} rows loaded. Beta uses one screening distance per batch; split mixed-exception markets into separate uploads.")
            if st.button("Run geographic screening",type="primary"):
                st.session_state.pop("screened",None)
                with st.spinner("Retrieving public OSM facilities and screening coordinates…"):
                    result, facilities, n, all_geometries, epsg = screen(data,lat_col,lon_col,id_col,feet)
                st.session_state["screened"] = result
                st.session_state["facilities"] = facilities
                st.session_state["source_count"] = n
                st.session_state["buffer"] = feet
                st.session_state["geometries"] = all_geometries
                st.session_state["epsg"] = epsg
        except Exception as exc:
            st.error(f"{type(exc).__name__}: {exc}")

if "screened" not in st.session_state:
    st.info("Upload a sanitized CSV/XLSX above to begin. A small example CSV is included in the downloadable project ZIP.")
    st.stop()

result=st.session_state["screened"]
facilities=st.session_state["facilities"]
st.divider()
st.subheader("2 · Review results")
counts=result["Screening Status"].value_counts()
metrics=st.columns(5)
for col,(label,key) in zip(metrics,[("Placements",None),("Red conflicts","Potential conflict"),("Yellow review","Needs more screening"),("University approval","University approval"),("No mapped hit","No mapped hit")]):
    col.metric(label,len(result) if key is None else int(counts.get(key,0)))
st.caption(f"{st.session_state['source_count']:,} mapped OSM source geometries assessed · {len(facilities):,} matched facilities · {st.session_state['buffer']} ft rule")
status_options=list(STATUS_COLORS)
selected_status=st.multiselect("Show screening statuses",status_options,default=status_options)
filtered=result[result["Screening Status"].isin(selected_status)].copy()
st.subheader("3 · Interactive footprint map")
mc1,mc2,mc3,mc4=st.columns(4)
show_red=mc1.checkbox("Red restricted buffers",True)
show_yellow=mc2.checkbox("University buffers",True)
show_sources=mc3.checkbox("Matched facility pins",True)
show_review=mc4.checkbox("50-foot review band",False)
st.caption("📏 Ruler: click the toolbar ruler, click a start point, move for live feet, click again to finish. Map buffers extend from mapped facility footprints, not address centers.")
if not filtered.empty:
    center=[float(filtered["Latitude"].median()),float(filtered["Longitude"].median())]
    m=folium.Map(location=center,zoom_start=13,tiles="CartoDB dark_matter",control_scale=True,prefer_canvas=True)
    m.add_child(LiveRuler())
    clusters={}
    for status,color in [("Potential conflict","#eb4753"),("Needs more screening","#f5be38"),("University approval","#f5be38"),("No mapped hit","#3dc08f")]:
        grp=folium.FeatureGroup(name=status,show=True)
        for _,r in filtered[filtered["Screening Status"]==status].iterrows():
            desc=(f"<b>{str(r['Placement ID'])}</b><br>{status}<br>Facility: {str(r['Matched Facility'])}"
                  f"<br>Distance: {r['Distance to Facility (ft)']} ft<br>OSM: {r['Matched OSM ID']}")
            folium.CircleMarker([r["Latitude"],r["Longitude"]],radius=6,color="white",weight=1,fill=True,fill_color=color,fill_opacity=.95,tooltip=str(r["Placement ID"]),popup=folium.Popup(desc,max_width=350)).add_to(grp)
        grp.add_to(m)
    # Only draw buffers around relevant sources near the displayed placements.
    # Use projected footprint geometries (meters), not point circles.
    projected_to_ll=Transformer.from_crs(f"EPSG:{st.session_state['epsg']}","EPSG:4326",always_xy=True).transform
    pts=[Point(float(r["Longitude"]),float(r["Latitude"])) for _,r in filtered.iterrows()]
    ll_to_projected=Transformer.from_crs("EPSG:4326",f"EPSG:{st.session_state['epsg']}",always_xy=True).transform
    active_pts=[transform(ll_to_projected,pt) for pt in pts]
    search_area=unary_union(active_pts).buffer((st.session_state['buffer']+150)*.3048)
    red_layer=folium.FeatureGroup(name="Restricted footprint buffers",show=show_red)
    yellow_layer=folium.FeatureGroup(name="University footprint buffers",show=show_yellow)
    band_layer=folium.FeatureGroup(name="50-foot review bands",show=show_review)
    displayed=0
    for f in st.session_state["geometries"]:
        if f["geometry_quality"] != "footprint": continue
        geom=f["geometry"]
        if not geom.intersects(search_area): continue
        if displayed>=450:
            st.warning("Map polygon preview capped at 450 nearby facilities for performance. Screening still considered all retrieved facilities.")
            break
        displayed+=1
        buffered=geom.buffer(st.session_state['buffer']*.3048, resolution=8)
        is_uni=f["category"]=="University approval"
        target=yellow_layer if is_uni else red_layer
        color="#f5be38" if is_uni else "#eb4753"
        # Simplification affects map display ONLY, not screening geometry.
        visual=transform(projected_to_ll,buffered.simplify(1.5,preserve_topology=True))
        folium.GeoJson(mapping(visual),style_function=lambda feature,c=color:{"color":c,"weight":1.6,"fillColor":c,"fillOpacity":.13},tooltip=f"{f['name']} · {f['category']} · {f['key']} · footprint buffer").add_to(target)
        if show_review:
            outer=geom.buffer((st.session_state['buffer']+50)*.3048,resolution=8)
            ring=outer.difference(buffered)
            folium.GeoJson(mapping(transform(projected_to_ll,ring.simplify(1.5,preserve_topology=True))),style_function=lambda feature:{"color":"#f5be38","weight":.7,"fillColor":"#f5be38","fillOpacity":.08}).add_to(band_layer)
    red_layer.add_to(m);yellow_layer.add_to(m);band_layer.add_to(m)
    if show_sources and not facilities.empty:
        source_layer=folium.FeatureGroup(name="Matched OSM facility pins",show=True)
        for _,f in facilities.iterrows():
            folium.CircleMarker([f["Latitude"],f["Longitude"]],radius=4,color="#f1f5f9",weight=1,fill=True,fill_color="#151515",fill_opacity=1,
                popup=f"{f['Facility Name']} · {f['Facility Category']} · {f['OSM ID']} · {f['Geometry Quality']}").add_to(source_layer)
        source_layer.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    st_folium(m,height=565,width=None,returned_objects=[],key="map_v5")
    st.caption(f"Displayed {displayed} nearby mapped footprint buffers. Incomplete geometries and point-only OSM sources are not drawn as authoritative footprint polygons. Screening uses unsimplified geometries.")
else:st.info("No placements match these filters.")
st.caption("Green = no detected intersection with retrieved OSM sources, NOT clearance. The map is a visual investigation aid; verify OSM footprint completeness and applicable legal rules.")
st.write("**Edit recommendations below.** Changes persist during this browser session and drive the client export.")
edit_cols=["Placement ID","Screening Status","Matched Facility","Distance to Facility (ft)","Recommendation","Reviewer Notes"]
edited=st.data_editor(result[edit_cols],hide_index=True,use_container_width=True,num_rows="fixed",
    disabled=["Placement ID","Screening Status","Matched Facility","Distance to Facility (ft)"],
    column_config={"Recommendation":st.column_config.SelectboxColumn("Recommendation",options=["Pending","Recommend","Exclude","Needs verification","Client approval"],required=True),"Reviewer Notes":st.column_config.TextColumn("Reviewer Notes",width="large")},key="review_editor")
result["Recommendation"]=edited["Recommendation"].values
result["Reviewer Notes"]=edited["Reviewer Notes"].values
st.session_state["screened"]=result

st.divider()
st.subheader("4 · Export for Google My Maps")
recommended=result[result["Recommendation"]=="Recommend"].copy()
st.write(f"**{len(recommended)} placements marked Recommend**. Only those placements go into the client-facing recommendation CSV.")
export_cols=["Placement ID","Latitude","Longitude","Screening Status","Recommendation","Reviewer Notes","Matched Facility","Facility Category","Distance to Facility (ft)"]
client=recommended[export_cols]
files={"01_All_Screening_Results.csv":csv_bytes(result),"02_Matched_OSM_Facilities.csv":csv_bytes(facilities),"03_MyMaps_Recommended_Placements.csv":csv_bytes(client)}
archive=io.BytesIO()
with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
    for filename,payload in files.items():z.writestr(filename,payload)
    z.writestr("README.txt","BETA preliminary screening. Google My Maps: import 03_MyMaps_Recommended_Placements.csv as a layer; choose Latitude and Longitude; style by Screening Status. Facility reference points are in file 02. A green point is not legal clearance. No Google My Maps project is automatically created.\n")
st.download_button("Download complete review package (.zip)",archive.getvalue(),file_name="ABI_OOH_Quick_Check_Review.zip",mime="application/zip",type="primary")
st.download_button("Download recommended placements for My Maps (.csv)",csv_bytes(client),file_name="MyMaps_Recommended_Placements.csv",mime="text/csv")
st.caption("My Maps: import CSV → select Latitude and Longitude → choose Placement ID as title → style by Screening Status. You will still publish/share the My Maps project manually.")
