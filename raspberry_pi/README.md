# Raspberry Pi healthy-confidence client

`pi_upload.py` uses Python 3.10+ and only its standard library. The Pi does not need the OpenAI key or the website's server packages. It makes outbound HTTPS requests, so no port forwarding is needed. This client requires the updated healthy-confidence API on https://ece-pet-health.onrender.com. Replace your previous pi_upload.py with this version; existing pet credentials still work.

## Recommended: a credential for one pet

1. Log into the website and select your pet.
2. Under **Raspberry Pi access**, create a device credential. Confirm your account password, name the device, and choose an expiry (up to 90 days). Copy the credential immediately; it is shown once. Pet ID is displayed in that section.
3. Download `pi_upload.py` to the Pi. Keep the credential in a private file, not in this script or shell command history:

```bash
mkdir -p ~/.config/pet-image
chmod 700 ~/.config/pet-image
umask 077
nano ~/.config/pet-image/device-token
# Paste just the credential on one line, save, and exit.
chmod 600 ~/.config/pet-image/device-token
```

Upload an existing image (replace the URL with your deployed site):

```bash
python3 pi_upload.py photo.jpg --server https://ece-pet-health.onrender.com
```

Optionally supply `--pet-id 12` as an extra check. The credential always determines the permitted pet; specifying a different ID is rejected. Success prints only a healthy-confidence percentage, for example `87.5%`. `upload_image()` returns that same Python string. Errors go to stderr with a nonzero exit status. The response is `text/plain`, with no analysis, disease list, HTML, or JSON envelope. The full explanation and JSON are still saved under the pet on the website. The percentage is an uncalibrated model estimate from visible evidence, not a medically validated probability that the pet is healthy.

To use the reply in another Python program:

```python
from pathlib import Path
from pi_upload import validate_server, read_token, upload_image

server = validate_server("https://ece-pet-health.onrender.com")
token = read_token(Path.home() / ".config/pet-image/device-token")
answer = upload_image(server, token, Path("photo.jpg"))
print(answer)  # e.g. "87.5%" (a Python str)
healthy_confidence = float(answer.removesuffix("%"))  # e.g. 87.5
```

In your existing camera-scanning program, call `upload_image()` after it has selected and saved a good frame. You can reuse the server and token above. Only the upload call waits for analysis; put it in a worker if your camera loop must keep scanning.

## Capture and upload with a Pi camera

On a Pi with a configured camera and `rpicam-still` installed:

```bash
python3 pi_upload.py --capture --server https://ece-pet-health.onrender.com
```

This captures a 1920×1080 JPEG without a preview, uploads it, and deletes the temporary photo after the request. See the [official Raspberry Pi camera documentation](https://www.raspberrypi.com/documentation/accessories/camera-software.html) for camera setup. Existing images can use the server's full 10 MB / 20-million-pixel limits.

## Interactive username/password option

For manual use, provide a username and the account's pet name or ID. The password is prompted without echo and is never stored by the script:

```bash
python3 pi_upload.py photo.jpg --server https://ece-pet-health.onrender.com --username YOUR_USERNAME --pet-name Fido
# Or use --pet-id 12 instead of --pet-name.
```

The script exchanges those credentials for a one-hour token scoped to that pet, uploads the image, and revokes the token before exiting. If revocation cannot reach the server, the token expires within one hour. Use a private device credential for unattended work.

## API contract

| Request | Authentication and result |
| --- | --- |
| `POST /api/login` | JSON `{username, password, pet_id}` or `{username, password, pet_name}`; returns JSON `{access_token, token_type, expires_at, pet_id}`. `expires_at` is a Unix UTC timestamp. This does not create a browser session. |
| `GET /api/pets` | Bearer credential; returns only its permitted pet in `{"pets":[{"id":12,"name":"Fido"}]}`. |
| `POST /api/images` | Bearer credential plus multipart field `image`; optional `pet_id` must match the credential. Returns status 200 and only the healthy-confidence percentage, e.g. `87.5%`, as `text/plain; charset=utf-8`. `X-Response-ID` identifies the saved response. |
| `POST /api/logout` | Bearer credential; revokes that credential and returns plain text. |

Errors have non-2xx status codes and plain-text bodies: 400 invalid image/request, 401 invalid/expired/revoked credential, 403 pet mismatch or browser Origin header, 413 oversize, 422 confidence unavailable, 429 shared quota, and 502/503 analysis service errors. A 422 means the model could not supply a valid healthy-confidence estimate (including inadequate images, refusals, or incomplete replies); the full result is saved and `X-Response-ID` identifies it. No invented percentage or disease-score subtraction is used. Do not retry a 422 automatically. The response has `Cache-Control: no-store`. API endpoints ignore browser cookies and do not accept browser-origin requests; browser forms still require CSRF protection.

Every accepted image uses the same analysis path as the website, including the pet's last 30 JSON results with UTC timestamps, per-account/global quotas, and encrypted hosted storage. Full results appear under that pet on the website. Other pets' results are not sent as context.

## Operational notes

- TLS certificates are verified. Remote HTTP and all redirects are rejected. Only loopback HTTP is allowed for local developer tests; do not disable certificate verification.
- No automatic upload retry is performed: a timeout can happen after the server has already saved a result. Check the website before resubmitting to avoid duplicate charges/results.
- Revoke credentials from the selected pet's page if a Pi is lost or a token is exposed. Removing a pet invalidates its credentials. Credentials expire automatically and only their hashes are stored by the server.
- The client rejects older full-message responses or malformed percentages, so an outdated server cannot be mistaken for a score. The Pi clock and CA certificates must be current. This code was tested against the server with mocked model responses; physical camera capture requires testing on your Pi.
- For a timer or automation, run the script as an unprivileged user with access to the camera and its private token file. `PET_SERVER_URL` can supply the server origin instead of `--server`. Keep credentials out of command-line arguments and logs.
