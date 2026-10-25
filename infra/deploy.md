# Deploying to AWS (same pattern as Module 1)

Before you start: move your history into Supabase (README, "Moving to Supabase and AWS").

## Option A — AWS Console (simplest, no extra tools)

1. Build the package: `python scripts/build_lambda.py` → `build/lambda.zip`
2. AWS Console → **Lambda** → **Create function** → Author from scratch
   - Runtime **Python 3.12**, Architecture **x86_64**
3. **Code** → **Upload from** → **.zip file** → choose `build/lambda.zip`
4. **Runtime settings** → Edit → Handler: `visa_monitor.handler.lambda_handler`
5. **Configuration** → **General**: Timeout **15 min**, Memory **1024 MB**
6. **Configuration** → **Environment variables**:

   | Key | Value |
   |---|---|
   | `STORAGE_BACKEND` | `postgres` |
   | `DATABASE_URL` | Supabase Transaction pooler URI (port 6543) |
   | `OPENAI_API_KEY` | your key |
   | `TIMEZONE` | `Australia/Melbourne` |
   | `ENABLE_JS_RENDER` | `false` |
   | `ALERT_WEBHOOK_URL` | optional, Teams/Slack webhook |
   | `EXPORT_BUCKET` | optional, S3 bucket for the CSV export |

7. **Test** tab → run with `{}` → it should return `statusCode: 200`.
8. Schedule it: **EventBridge** → **Scheduler** → **Create schedule**
   - Recurring, cron `0 7 * * ? *`, **time zone Australia/Melbourne**
   - Target: AWS Lambda → your function. Retry policy: 2 attempts.

## Option B — SAM (infrastructure as code)

```bash
python scripts/build_lambda.py        # creates build/lambda/ and build/lambda.zip
cd infra
sam deploy --guided                   # enter DatabaseUrl, OpenAiApiKey, AlertWebhookUrl
```
`template.yaml` points at `build/lambda/`, creates the S3 export bucket, the 7:00 am
Melbourne schedule and a CloudWatch alarm if a run errors.

Test once by hand: `aws lambda invoke --function-name <FunctionName> out.json && cat out.json`

The schedule uses **EventBridge Scheduler with time zone Australia/Melbourne**, so it fires
at 7:00 am Melbourne time all year, including across daylight-saving changes. The code also
stamps each record with the Melbourne date, never the UTC date.

## JavaScript-rendered pages
The zip package has no browser, so pages that build their content in JavaScript (e.g.
processing times) return little text on Lambda and the run logs a warning. To capture them:
- deploy as a **container image** using `infra/Dockerfile.playwright` and set `ENABLE_JS_RENDER=true`, or
- find the JSON/CSV endpoint the page loads (browser DevTools → Network tab) and add it to
  `sources.yaml` as a static source, or
- keep running those sources from your computer (`run_daily.py` with Playwright).
