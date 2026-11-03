# ABI OOH LDA Coverage Generator — v2

Public-data-only Streamlit proof of concept. No campaign inventory uploads.

## Update via GitHub
1. Replace `streamlit_app.py`, `requirements.txt`, and `README.md` in the root of your existing repository.
2. For the dark theme, use GitHub **Add file → Create new file** and name it `.streamlit/config.toml`; paste the contents of this package's `.streamlit/config.toml`, then commit.
3. In Streamlit Community Cloud's app settings, select Python **3.12** if offered. Python version is not controlled by `requirements.txt` and changing it may require redeployment.
4. Reload the app. The initial page now renders without fetching geographic data. Click **Load DMA markets**, then select a DMA and generate a KMZ.

If the app still hangs before displaying the initial interface, inspect the Streamlit frontend/browser network console and logs. Successful server startup does not guarantee browser assets load.

## Important limitations
- Prototype only: one DMA at a time; public Overpass servers may reject large requests.
- Some OSM relation/line geometries are represented by point proxies.
- This does not reproduce Colab's full screening logic or Google Earth section splitting.
- A missing red zone must never be interpreted as legal clearance.
