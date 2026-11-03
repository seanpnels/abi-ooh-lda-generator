# ABI OOH LDA Coverage Generator — Streamlit proof of concept

A public-data-only experimental generator for preliminary geographic review. **No proprietary placement data is uploaded or processed.**

## Deploy to Streamlit Community Cloud

1. In GitHub, open your **public** `abi-ooh-lda-generator` repository.
2. Select **Add file → Upload files**. Unzip this package on your computer, and upload `streamlit_app.py`, `requirements.txt`, and `README.md` to the repository root. **Also create** `.streamlit/config.toml` with the supplied content if you want the matching dark theme; GitHub's web uploader may not preserve the hidden `.streamlit` folder.
3. Click **Commit changes**.
4. At https://share.streamlit.io, choose **Create app → Deploy a public app from GitHub**, select your repository, branch `main`, and entrypoint `streamlit_app.py`.
5. Wait for dependencies to install, then open your app URL.

## What this first prototype does

- Fetches and validates all 209 DMA boundaries from the same Esri 2025 service used in the Colab notebook.
- Uses public Overpass servers to request candidate OSM schools, childcare, worship, and certain addiction-treatment tags.
- Applies 500-ft or 250-ft metric buffers, clips to selected DMA, and exports a Google Earth KMZ.
- Refuses to generate an empty result after an unsuccessful query.

## Important limitations

- **This is a proof of concept, not a drop-in replacement for the validated Colab tool.** Overpass queries are not equivalent to the prior state-PBF extraction. Results must be compared and verified before any real compliance decision.
- One DMA at a time; no oversized-file tiling or multi-DMA bundling yet.
- Public Overpass servers have quotas, latency, and timeouts; large DMAs may fail. This prototype deliberately fails closed rather than silently treating an incomplete request as a clean DMA.
- Some incomplete OSM way/relation geometries are approximated as points, potentially changing buffered shapes. Not suitable as final clearance.
- Streamlit free hosting can sleep and has CPU/RAM limits. No guaranteed uptime or persistent data cache.
- No company trademarks, private credentials, proprietary grids, or internal client files should be uploaded to a public GitHub repository.
