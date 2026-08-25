# Quickstart

## Local (do this first)

```bash
unzip jobpipe.zip && cd jobpipe
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Paste your key into `.env`, then:

```bash
python scripts/doctor.py
```

It reports your tier, lists the models your key can actually call, and prints
the exact `MODEL_SCORE` / `MODEL_TAILOR` / `GEMINI_RPM` lines to paste back into
`.env`. Apply them, then re-run doctor until config checks are green.

Fix `config/facts.yaml` (doctor will tell you it's 0/16 verified), then:

```bash
export PYTHONPATH=src
python -m jobpipe.cli verify-sources   # prune dead ATS slugs
python -m jobpipe.cli ingest
python -m jobpipe.cli score
python -m jobpipe.cli prepare
make review                            # localhost:8080
```

## OCI

```bash
# from your laptop
rsync -av --exclude .venv --exclude data --exclude .git \
  ./jobpipe/ ubuntu@<your-oci-ip>:~/jobpipe/

# on the box
cd ~/jobpipe && bash deploy/oci-setup.sh
```

Then add the ingress block from `cloudflared-config.example.yml` to your tunnel
and put the hostname behind Cloudflare Access.

## Vercel

See `VERCEL.md`. Short answer: not for the pipeline.
