# Cluster jobs

The PBS files in this directory serve the runtime model and run the optional
external Retail evaluation. They assume a separately checked-out external
environment at the pinned commit in `docs/data_source_manifest.json`.

| Job | Use |
|---|---|
| `serve_tau3_agent_v1.pbs` | serve the configured agent model |
| `run_tau3_retail_base_v1.pbs` | run the cluster-side Retail evaluation helper |
| `download_models.py` | prepare model assets on the cluster |

Prepare a clean environment, install `requirements.txt`, and run the CPU-side
configuration and import checks before submitting a job. All logs, reports,
and model outputs must be placed on the cluster data volume, not the system
volume. Do not submit these jobs as part of the local validation gate.
