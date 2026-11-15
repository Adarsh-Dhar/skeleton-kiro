# Steering: domain independence
- Domain knowledge (F1, energy, coffee, ...) lives only in adapters and examples, built on `from trialforge import ...`.
- Synthetic or simulated data must be labelled: `AdapterMetadata.synthetic=True`, `Measurement.metadata["synthetic"]=True`, a source string, and the report limitation notice.
- Never claim a connection to real systems (teams, telemetry feeds, meters) that does not exist.
- A new domain must work by registering an adapter; if it needs a core change, treat that as a design bug in the core.
