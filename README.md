OrientationFieldHarvesterV1

A tiny Python prototype that: accepts a DOI, URL, or path to a local PDF; attempts to extract candidate supplemental URLs from the HTML (or local PDF text if pdftotext exists); downloads candidate files; validates CSV/JSON/NumPy (.npy) time-series for a minimal pose schema; aligns timestamps and writes merged CSV plus a provenance JSON. No external services are called by the script except the URLs you provide.

Usage:

python3 main.py --source "https://doi.org/10.XXXX/XXXX" --out outdir

Notes:
- This is a proof-of-concept. It uses only Python standard library. If you want nicer HTTP handling, install requests and uncomment the import in the file.
- Validation is minimal: expects records with timestamp, module_id, x,y,z and either quaternion (qx,qy,qz,qw) or euler (roll,pitch,yaw).
- See main.py for details and limitations.