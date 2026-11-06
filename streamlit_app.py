"""ABI OOH LDA Quick Check beta. Public OSM screening, no authentication yet."""
import io
import math
import re
import zipfile
from datetime import datetime, timezone

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
from pyproj import CRS, Transformer
from shapely.geometry import Point, Polygon
from shapely.ops import transform

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
    if e.get("type") == "node": return Point(float(e["lon"]), float(e["lat"]))
    points = [(p["lon"], p["lat"]) for p in e.get("geometry", []) if "lon" in p and "lat" in p]
    if len(points) >= 4 and points[0] == points[-1]:
        poly = Polygon(points)
        if not poly.is_valid: poly = poly.buffer(0)
        if not poly.is_empty: return poly
    # Incomplete ways/relations are excluded rather than incorrectly considered precise footprints.
    return None


def overpass_query(bbox):
    s, w, n, e = bbox
    selector = "".join(f'nwr["{k}"="{v}"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});' for k, vals in CATEGORIES.items() for v in vals)
    return "[out:json][timeout:150];(" + selector + ");out geom;"


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
    records = df.copy().reset_index(drop=True)
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
        g = osm_geometry(e)
        if g is None: continue
        gm = transform(forward, g)
        if gm.is_empty: continue
        pt = g.representative_point()
        facilities.append({"key": f"{key[0]}/{key[1]}", "name": e.get("tags", {}).get("name", "Unnamed OSM facility"), "category": cat,
                           "geometry": gm, "lat": pt.y, "lon": pt.x})
    if not facilities: raise ValueError("No usable facility geometries returned. Refusing to classify placements as green.")
    rows=[]; matched={}
    for i, point in enumerate(xy):
        nearby=[]
        for f in facilities:
            distance = point.distance(f["geometry"]) / 0.3048
            if distance <= feet + 50:
                nearby.append((distance, f))
        nearby.sort(key=lambda x:x[0])
        red = [(d,f) for d,f in nearby if d <= feet and f["category"] != "University approval"]
        uni = [(d,f) for d,f in nearby if d <= feet and f["category"] == "University approval"]
        band = [(d,f) for d,f in nearby if feet < d <= feet+50]
        if red: status="Potential conflict"; relevant=red
        elif uni: status="University approval"; relevant=uni
        elif band: status="Needs more screening"; relevant=band
        else: status="No mapped hit"; relevant=[]
        closest = relevant[0] if relevant else (nearby[0] if nearby else None)
        rows.append({"Screening Status":status, "Matched Facility":closest[1]["name"] if closest else "",
                     "Facility Category":closest[1]["category"] if closest else "",
                     "Distance to Facility (ft)":round(closest[0],1) if closest else None,
                     "Matched OSM ID":closest[1]["key"] if closest else "",
                     "Recommendation":"Pending", "Reviewer Notes":""})
        for distance, f in relevant:
            matched.setdefault(f["key"], {"Facility Name":f["name"], "Facility Category":f["category"], "OSM ID":f["key"],
                                         "Latitude":f["lat"], "Longitude":f["lon"], "Affected Placement IDs":set()})
            matched[f["key"]]["Affected Placement IDs"].add(records.loc[i,"Placement ID"])
    output = pd.concat([records, pd.DataFrame(rows)],axis=1)
    facility_rows=[]
    for f in matched.values():
        f["Affected Placement IDs"]="; ".join(sorted(f["Affected Placement IDs"]))
        facility_rows.append(f)
    return output, pd.DataFrame(facility_rows, columns=["Facility Name","Facility Category","OSM ID","Latitude","Longitude","Affected Placement IDs"]), len(facilities)


def csv_bytes(df): return df.to_csv(index=False).encode("utf-8-sig")

st.caption("ABI OOH  /  LDA COMPLIANCE HUB  /  BETA v4")
st.title("Quick Check · Campaign Screening")
st.write("Screen sanitized placement coordinates, investigate mapped facilities, and prepare a Google My Maps recommendation export.")
st.warning("**Beta / preliminary only.** OSM is incomplete; green means no intersection with the retrieved mapped facilities, **not legal clearance**. University approval and the 50-foot review band are separate review categories. No authentication: use synthetic or explicitly approved data only.")

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
                    result, facilities, n = screen(data,lat_col,lon_col,id_col,feet)
                st.session_state["screened"] = result
                st.session_state["facilities"] = facilities
                st.session_state["source_count"] = n
                st.session_state["buffer"] = feet
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
view=filtered[["Placement ID","Latitude","Longitude","Screening Status","Matched Facility","Facility Category","Distance to Facility (ft)","Recommendation","Reviewer Notes"]].copy()
if not view.empty:
    view["color"]=view["Screening Status"].map(STATUS_COLORS)
    view["tooltip"] = view["Placement ID"].astype(str)+" · "+view["Screening Status"]+" · "+view["Matched Facility"].fillna("")
    layers=[pdk.Layer("ScatterplotLayer",view,get_position="[Longitude, Latitude]",get_fill_color="color",get_radius=40,radius_min_pixels=5,radius_max_pixels=13,pickable=True)]
    if not facilities.empty:
        facilities_map=facilities.copy();facilities_map["color"]=[[25,25,25,230]]*len(facilities_map)
        layers.append(pdk.Layer("ScatterplotLayer",facilities_map,get_position="[Longitude, Latitude]",get_fill_color="color",get_line_color="[240,240,240]",line_width_min_pixels=1,stroked=True,get_radius=28,radius_min_pixels=4,radius_max_pixels=9,pickable=True))
    st.pydeck_chart(pdk.Deck(map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",initial_view_state=pdk.ViewState(latitude=float(view["Latitude"].median()),longitude=float(view["Longitude"].median()),zoom=12,pitch=0),layers=layers,tooltip={"html":"<b>{Placement ID}{Facility Name}</b><br/>{Screening Status}{Facility Category}<br/>{Matched Facility}","style":{"backgroundColor":"#182434","color":"white"}}),height=510)
else:st.info("No placements match these filters.")
st.caption("Colored dots = placements; black dots = matched OSM facilities. Facility dots are representative points, not legal boundary markers.")
st.write("**Edit recommendations below.** Changes persist during this browser session and drive the client export.")
edit_cols=["Placement ID","Screening Status","Matched Facility","Distance to Facility (ft)","Recommendation","Reviewer Notes"]
edited=st.data_editor(result[edit_cols],hide_index=True,use_container_width=True,num_rows="fixed",
    disabled=["Placement ID","Screening Status","Matched Facility","Distance to Facility (ft)"],
    column_config={"Recommendation":st.column_config.SelectboxColumn("Recommendation",options=["Pending","Recommend","Exclude","Needs verification","Client approval"],required=True),"Reviewer Notes":st.column_config.TextColumn("Reviewer Notes",width="large")},key="review_editor")
result["Recommendation"]=edited["Recommendation"].values
result["Reviewer Notes"]=edited["Reviewer Notes"].values
st.session_state["screened"]=result

st.divider()
st.subheader("3 · Export for Google My Maps")
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
