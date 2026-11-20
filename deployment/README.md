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
