# ABI OOH LDA Generator v3

## Updating the existing deployment
Upload the new `streamlit_app.py`, `requirements.txt`, and `README.md` to the **root** of the existing GitHub repository, replacing the old files. In GitHub, open `.streamlit/config.toml` and replace its contents with the v3 file from this ZIP. Commit changes. Streamlit redeploys automatically.

## What changed
- No duplicate DMA loading spinners; boundaries load once on demand and cache for 24 hours.
- DMA markets sorted alphabetically.
- Select up to five DMAs. Multiple DMAs produce a ZIP with a separate KMZ for each DMA, **not** one merged KMZ. Each DMA runs sequentially; large selections can take a long time or exceed free hosting resources.
- Both a standard download button and a plain direct link to a temporary server-side copy. Static serving must be enabled in `.streamlit/config.toml`.

## Important
- Recovery link is public, ephemeral, and removed on restart/redeploy or after 24 hours. Do not put proprietary information in this app. Files are not durable across deployments.
- A JS dynamic-import error may still be a Streamlit-hosting/browser asset issue. The direct link is intended as a fallback, but has not been verified on Streamlit Community Cloud.
- Public OpenStreetMap preliminary screening only, not legal clearance. OSM tagging and geometry may be incomplete; relation/line geometries may use point proxies.
- No proprietary inventory upload functionality.
