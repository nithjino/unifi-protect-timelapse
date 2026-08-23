# Own daily automation per runtime

Daily automation policy will live in shared, deep Python engine and job-coordinator modules, with one process or app session owning each entrypoint's separate registry. CLI, Web, and Qt will run those modules in process; macOS and Windows will use them through one long-lived Python supervisor per app. We rejected a machine daemon because automations are intentionally active only while their owning entrypoint runs, and rejected Web-owned policy because Web persistence and presentation are not shared product rules.
