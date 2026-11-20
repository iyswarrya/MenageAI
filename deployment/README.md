# Google Cloud Run Deployment Guide

This guide describes how to build, deploy, and reproduce the deployment of the Family Receipt Agent (`menageai`) on Google Cloud Run.

---

## 1. Prerequisites

Before deploying, ensure you have the following installed and configured:
1. **Google Cloud CLI (`gcloud`)** installed and authenticated.
2. An active **Google Cloud Project** with billing enabled.
3. Your terminal authenticated with your user account:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project YOUR_PROJECT_ID
   ```

---

## 2. Required Google Cloud APIs

Enable the following APIs in your Google Cloud Project:
```bash
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    aiplatform.googleapis.com \
    discoveryengine.googleapis.com \
    --project=YOUR_PROJECT_ID
```

* **`run.googleapis.com`**: Hosts the web application.
* **`cloudbuild.googleapis.com`**: Handles building container images from source.
* **`artifactregistry.googleapis.com`**: Stores the compiled Docker container.
* **`aiplatform.googleapis.com`**: Enables access to Gemini models via Vertex AI.
* **`discoveryengine.googleapis.com`**: Required for integration with Gemini Enterprise / Antigravity Playground.

---

## 3. Required IAM Permissions

For the application to call Vertex AI models (Gemini) and allow the Antigravity Playground to discover the agent, grant these permissions:

### A. Vertex AI Access for the Cloud Run Instance
Run the following to allow the Cloud Run runtime service account to call Vertex AI:
```bash
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

### B. Gemini Enterprise Discovery Access
Allow the Discovery Engine service account to call your Cloud Run service:
```bash
gcloud run services add-iam-policy-binding menageai \
  --member="serviceAccount:service-YOUR_PROJECT_NUMBER@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
  --role="roles/run.servicesInvoker" \
  --region=us-west1
```

---

## 4. Environment Variables

Configure the following environment variables. In Cloud Run, these are set during deployment:

| Variable | Description | Example Value |
|---|---|---|
| `GOOGLE_GENAI_USE_VERTEXAI` | Enables enterprise Vertex AI endpoint routing | `true` |
| `GOOGLE_CLOUD_PROJECT` | Active Google Cloud Project ID | `YOUR_PROJECT_ID` |
| `GOOGLE_CLOUD_LOCATION` | Active Vertex AI location | `global` |
| `USE_MCP_DEALS` | Enable deals extraction and grounding validation | `true` |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID for WhatsApp. Mount from Secret Manager. | Secret Manager `TWILIO_ACCOUNT_SID` |
| `TWILIO_AUTH_TOKEN` | Twilio auth token. Used for the Messages API and for `X-Twilio-Signature` checks. Mount from Secret Manager only. Do not put the token in `--update-env-vars`, the container image, shell history, or git. | Secret Manager `TWILIO_AUTH_TOKEN` |
| `TWILIO_WEBHOOK_PUBLIC_URL` | Exact public HTTPS webhook URL configured in the Twilio console, including the path and any query string. SEC-02A validates signatures against this value and does not read `Host` or `X-Forwarded-*`. This URL is not a credential. | `https://YOUR_SERVICE_URL/twilio/webhook` |
| `TWILIO_MEDIA_MAX_BYTES` | Optional WhatsApp media download cap. Defaults to 10485760 (10 MB). Set with `--update-env-vars`. It is not a secret, and values above 10 MB are rejected. | `10485760` |
| `TWILIO_NUMBER` | Your Twilio Outbound WhatsApp Number | `whatsapp:Your Twilio Outbound WhatsApp Number` |
| `FAMILY_WHATSAPP_NUMBERS` | Comma-separated list of approved sender numbers | `whatsapp:approved sender number1`,`whatsapp:approved sender number2` |

### Twilio credentials and webhook URL

`TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are secrets. Create or rotate them from stdin so the values are not command arguments and do not land in shell history:

```bash
gcloud services enable secretmanager.googleapis.com --project=YOUR_PROJECT_ID

# First version. Paste the token, then press Ctrl-D. Do not pass it as a flag.
gcloud secrets create TWILIO_AUTH_TOKEN \
    --replication-policy=automatic \
    --data-file=-

# Later versions:
gcloud secrets versions add TWILIO_AUTH_TOKEN --data-file=-
```

Repeat that pattern for `TWILIO_ACCOUNT_SID`. Grant the Cloud Run runtime service account `roles/secretmanager.secretAccessor` on those secrets. Deploy mounts them with `--update-secrets`. Do not copy the token into `--update-env-vars` or `--set-env-vars`.

`TWILIO_WEBHOOK_PUBLIC_URL` is the public URL Twilio calls. Set it as a normal environment variable after the Cloud Run URL exists. `--update-env-vars` changes only the listed variables and leaves the mounted secrets in place:

```bash
gcloud run services update menageai \
    --region us-west1 \
    --update-env-vars "TWILIO_WEBHOOK_PUBLIC_URL=https://YOUR_SERVICE_URL/twilio/webhook"
```

Use the same string in the Twilio console webhook field, including a query string when there is one. If that string contains a comma, gcloud's default env-var delimiter will split it. Quote the value and set another delimiter, such as `@`:

```bash
gcloud run services update menageai \
    --region us-west1 \
    --update-env-vars "^@^TWILIO_WEBHOOK_PUBLIC_URL=https://YOUR_SERVICE_URL/twilio/webhook?ids=1,2"
```

Do not use `--set-env-vars` or `--clear-env-vars` for this update. Those flags replace the service environment and can drop the secret mounts. The webhook returns HTTP 403 until both `TWILIO_AUTH_TOKEN` and `TWILIO_WEBHOOK_PUBLIC_URL` are set and the URL matches Twilio exactly.

---

## 5. Build and Deploy Commands

Section 5 updates the existing Cloud Run service `menageai`. Use Section 7 for the isolated staging service `menageai-staging`. Do not point the Section 5 commands at `menageai-staging`, and do not point the Section 7 commands at `menageai`.

### Method A: Using Manual `gcloud` Commands (Recommended)

1. **Create the Artifact Registry Docker repository**:
   ```bash
   gcloud artifacts repositories create YOUR_PROJECT_REPO \
       --repository-format=docker \
       --location=us-west1 \
       --description="Docker repository"
   ```

2. **Submit the build to Cloud Build**:
   ```bash
   gcloud builds submit \
       --tag us-west1-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_PROJECT_REPO/menageai:latest
   ```

3. **Deploy to Cloud Run with public access enabled**:
   ```bash
   gcloud run deploy menageai \
       --image us-west1-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_PROJECT_REPO/menageai:latest \
       --region us-west1 \
       --allow-unauthenticated \
       --update-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,USE_MCP_DEALS=true,TWILIO_NUMBER=whatsapp:YOUR_TWILIO_OUTBOUND_WHATSAPP_NUMBER,FAMILY_WHATSAPP_NUMBERS=whatsapp:SENDER_NUMBER1,whatsapp:SENDER_NUMBER2,TWILIO_WEBHOOK_PUBLIC_URL=https://YOUR_SERVICE_URL/twilio/webhook" \
       --update-secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest
   ```

   On the first deploy the service URL is not known yet. Omit `TWILIO_WEBHOOK_PUBLIC_URL` from that command, then set it with the Section 4 `gcloud run services update` command. That update does not send `TWILIO_AUTH_TOKEN`.

---

### Method B: Using `agents-cli`

If using ADK's built-in deploy tool:
```bash
agents-cli deploy \
  --project YOUR_PROJECT_ID \
  --region us-west1 \
  --service-name menageai \
  --no-confirm-project \
  --secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN \
  --update-env-vars "FAMILY_WHATSAPP_NUMBERS=whatsapp:SENDER_NUMBER1,whatsapp:SENDER_NUMBER2,TWILIO_NUMBER=whatsapp:YOUR_TWILIO_OUTBOUND_WHATSAPP_NUMBER,TWILIO_WEBHOOK_PUBLIC_URL=https://YOUR_SERVICE_URL/twilio/webhook"
```

---

## 6. How to Reproduce the Deployment

Follow these sequential steps to reproduce this exact deployment in a clean project:

1. **Clone/Checkout the Repository** in your workspace.
2. **Log in** to your target GCP Project and enable the required APIs (listed in Section 2).
3. Store `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in Secret Manager (Section 4). Do not place the auth token in an env file that is deployed with the image.
4. Run the **Build** command to package and submit the container to Artifact Registry.
5. Run the **Deploy** command. Once completed, note the generated service URL.
6. Run the IAM command in Section 3B to allow Gemini Enterprise to invoke your service.
7. Open the generated Service URL in an **incognito window** to test the web client.
8. Set `TWILIO_WEBHOOK_PUBLIC_URL` to the service webhook URL with the `gcloud run services update` command in Section 4. Use that same exact URL as the Twilio console WhatsApp webhook. Signature checks fail closed until the two strings match. The update command sets only the public URL and does not resend `TWILIO_AUTH_TOKEN`.

---

## 7. Isolated staging service (`menageai-staging`)

This section prepares a separate Cloud Run service in project `winter-field-500315-i4`, region `us-west1`. It does not change the existing service `menageai` and it does not change the Twilio Sandbox. Run the cloud commands only after a separate approval. Do not copy the `menageai` service specification into the staging deploy: that specification stores Twilio credentials as ordinary environment values, and copying it would place those values in the command line and in shell history.

Checked without enabling APIs or reading values:

- `secretmanager.googleapis.com` is not enabled, so Secret Manager secrets cannot be listed or mounted yet.
- `menageai` does not reference Secret Manager for `TWILIO_ACCOUNT_SID` or `TWILIO_AUTH_TOKEN`.
- Service account `menageai-staging@winter-field-500315-i4.iam.gserviceaccount.com` is not present.
- Cloud Run service `menageai-staging` is not present.
- Artifact Registry repository `menageai-repo` is present in `us-west1`.

### Staging limits

The container creates `app/family_receipts.db` on startup when the file is missing. Cloud Run has no volume for that file. The database lives on the instance filesystem and is discarded when the instance is replaced, scaled to zero, or moved to a new revision. Receipts, household rows, and ADK sessions from the previous instance are gone. Sessions stay in process memory.

`FAMILY_WHATSAPP_NUMBERS` provisions household `twilio_household` into whatever database file that instance has. On a fresh file, each listed number becomes a new active member. A revocation stored in an earlier instance is not in the new file, so startup creates an active membership again. Revocation survives only as long as that same SQLite file survives.

The webhook returns empty TwiML and continues the agent, media download, and outbound WhatsApp send in a FastAPI `BackgroundTasks` task. CPU is always allocated on this staging service so that work can keep running after the HTTP response while the instance is still up. Completion is not guaranteed during instance termination: scale-to-zero, a new revision, or a Cloud Run shutdown stops the task. Twilio already received HTTP 200, so it does not retry the webhook. A missing WhatsApp reply after a 200 in the Twilio debugger means the background work was dropped.

Staging uses one instance and concurrency 1 so that single SQLite file has one writer. The image tag is separate from the production image:

`us-west1-docker.pkg.dev/winter-field-500315-i4/menageai-repo/menageai-staging:sec02`

`.dockerignore` keeps SQLite databases, `.env` files, credential files, virtual environments, Git metadata, caches, and local receipt data out of the build context. The Dockerfile copies `pyproject.toml`, `README.md`, `app/`, and `mcp_server/` only. It does not name a database or a secret file.

### Check Twilio secrets before creating them

Enable Secret Manager only when staging is approved. Then list names. Do not access secret versions and do not print payloads.

```bash
gcloud services list --enabled \
    --project=winter-field-500315-i4 \
    --filter="config.name:secretmanager.googleapis.com" \
    --format="value(config.name)"

gcloud secrets list \
    --project=winter-field-500315-i4 \
    --format="table(name)"
```

For each of `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`, create the secret only when describe reports that the name is absent. Paste the value from the Twilio console, then press Ctrl-D. If the name already exists, skip both `secrets create` and `secrets versions add`.

```bash
gcloud secrets describe TWILIO_AUTH_TOKEN --project=winter-field-500315-i4 >/dev/null

gcloud secrets create TWILIO_AUTH_TOKEN \
    --project=winter-field-500315-i4 \
    --replication-policy=automatic \
    --data-file=-
```

Repeat the describe-then-create check for `TWILIO_ACCOUNT_SID`.

### Staging service account and deploy

Create a dedicated runtime account. Grant it Vertex AI user on the project and Secret Manager accessor on the two Twilio secrets. Do not change the service account or IAM of `menageai`.

```bash
gcloud iam service-accounts create menageai-staging \
    --project=winter-field-500315-i4 \
    --display-name="MenageAI staging"

gcloud projects add-iam-policy-binding winter-field-500315-i4 \
    --member="serviceAccount:menageai-staging@winter-field-500315-i4.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"

for SECRET in TWILIO_AUTH_TOKEN TWILIO_ACCOUNT_SID; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
      --project=winter-field-500315-i4 \
      --member="serviceAccount:menageai-staging@winter-field-500315-i4.iam.gserviceaccount.com" \
      --role="roles/secretmanager.secretAccessor"
done

gcloud builds submit \
    --project=winter-field-500315-i4 \
    --tag us-west1-docker.pkg.dev/winter-field-500315-i4/menageai-repo/menageai-staging:sec02

gcloud run deploy menageai-staging \
    --project=winter-field-500315-i4 \
    --image us-west1-docker.pkg.dev/winter-field-500315-i4/menageai-repo/menageai-staging:sec02 \
    --region us-west1 \
    --service-account menageai-staging@winter-field-500315-i4.iam.gserviceaccount.com \
    --allow-unauthenticated \
    --cpu=1 \
    --memory=512Mi \
    --timeout=300 \
    --concurrency=1 \
    --max-instances=1 \
    --no-cpu-throttling \
    --update-secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest \
    --update-env-vars "^@^GOOGLE_GENAI_USE_VERTEXAI=true@GOOGLE_CLOUD_PROJECT=winter-field-500315-i4@GOOGLE_CLOUD_LOCATION=global@USE_MCP_DEALS=false@TWILIO_NUMBER=whatsapp:YOUR_TWILIO_NUMBER@FAMILY_WHATSAPP_NUMBERS=whatsapp:+15551111111,whatsapp:+15552222222"
```

`--no-cpu-throttling` leaves CPU allocated while the instance is running, which is what lets the webhook background task finish after the TwiML response. `--allow-unauthenticated` is required because Twilio does not send a Google identity. The webhook still rejects a missing or invalid `X-Twilio-Signature`.

The first deploy omits `TWILIO_WEBHOOK_PUBLIC_URL`. After the service exists, set that variable on `menageai-staging` only:

```bash
STAGING_URL="$(gcloud run services describe menageai-staging \
    --project=winter-field-500315-i4 \
    --region us-west1 \
    --format='value(status.url)')"

gcloud run services update menageai-staging \
    --project=winter-field-500315-i4 \
    --region us-west1 \
    --update-env-vars "^@^TWILIO_WEBHOOK_PUBLIC_URL=${STAGING_URL}/twilio/webhook"
```

Use `--update-env-vars` so the secret mounts stay in place. The value must be the exact URL Twilio will call, including the path and any query string.

### Staging smoke test

Record the current Twilio Sandbox "When a message comes in" URL before changing it. That exact string is the restore target in the next section. Leave the Sandbox on the original URL until the staging service is up and its webhook URL is set.

Confirm the new service shape without printing environment values:

```bash
gcloud run services describe menageai-staging \
    --project=winter-field-500315-i4 \
    --region us-west1 \
    --format="yaml(metadata.name,spec.template.spec.containerConcurrency,spec.template.spec.serviceAccountName,spec.template.metadata.annotations['autoscaling.knative.dev/maxScale'],spec.template.metadata.annotations['run.googleapis.com/cpu-throttling'])"
```

Expect service `menageai-staging`, concurrency `1`, max scale `1`, CPU throttling `false`, and the dedicated staging service account.

Then:

1. Send an unsigned POST to `https://<menageai-staging-host>/twilio/webhook`. Expect HTTP 403 and no WhatsApp message.
2. Point the Twilio Sandbox webhook at `${STAGING_URL}/twilio/webhook` for the test window only. A valid Sandbox message returns empty TwiML and HTTP 200. The Twilio debugger is the signature check. A 403 means the staging URL and `TWILIO_WEBHOOK_PUBLIC_URL` differ.
3. From a number in `FAMILY_WHATSAPP_NUMBERS`, send one JPEG, PNG, or WebP receipt. The sender receives a private reply that reflects the extracted receipt.
4. A second number in that household receives a broadcast only after the receipt is stored for this run. A repeated copy of the same receipt stays private to the sender and does not broadcast again.
5. Send an unsupported file, or a receipt that fails download. The sender gets a private failure reply. No successful-receipt broadcast goes out.
6. Ask a purchase question. The answer goes only to the sender.
7. If Twilio shows HTTP 200 and no WhatsApp reply arrives, the background task ended with the instance. Send the message again after confirming the instance is still running. Twilio will not retry the original webhook.

### Restore the Twilio Sandbox webhook

Restoring the Sandbox returns inbound Sandbox messages to the original service. It does not modify the `menageai` Cloud Run service.

1. Open Twilio Console → Messaging → Try it out → Send a WhatsApp message → Sandbox settings.
2. Replace "When a message comes in" with the URL recorded before the smoke test. Use that recorded string, including its path and query string.
3. If the recorded string is missing, read only the public service URL and append the webhook path:

   ```bash
   gcloud run services describe menageai \
       --project=winter-field-500315-i4 \
       --region us-west1 \
       --format='value(status.url)'
   ```

   Set the Sandbox webhook to `https://<that-host>/twilio/webhook`. The `menageai` service does not currently store `TWILIO_WEBHOOK_PUBLIC_URL`, so the recorded console value is the source of truth when it exists.
4. Save the Sandbox settings. Send one Sandbox message and confirm the request appears on `menageai` and no longer on `menageai-staging`.
5. Leave `menageai-staging` in place. Do not run `gcloud run services update menageai`.
