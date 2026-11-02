# Raspberry Pi remote image analysis

`pi_upload.py` uses Python 3.10+ and only its standard library. The Pi does not need the OpenAI key or the website's server packages. It makes outbound HTTPS requests, so no port forwarding is needed. The website must be running the version containing this API at a reachable HTTPS origin, such as your actual Render hostname.

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

Optionally supply `--pet-id 12` as an extra check. The credential always determines the permitted pet; specifying a different ID is rejected. Success prints only the model's reply to stdout. Errors go to stderr with a nonzero exit status. The response is a `text/plain` string, with no HTML or JSON response envelope. It can contain line breaks or a JSON section if the model includes those in its reply.

To use the reply in another Python program:

```python
from pathlib import Path
from pi_upload import validate_server, read_token, upload_image

server = validate_server("https://ece-pet-health.onrender.com")
token = read_token(Path.home() / ".config/pet-image/device-token")
answer = upload_image(server, token, Path("photo.jpg"))
print(answer)  # A Python str. Treat it as data; do not execute it as a shell command.
```

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
| `POST /api/images` | Bearer credential plus multipart field `image`; optional `pet_id` must match the credential. Returns status 200 and the saved model reply as `text/plain; charset=utf-8`. `X-Response-ID` identifies the saved response. |
| `POST /api/logout` | Bearer credential; revokes that credential and returns plain text. |

Errors have non-2xx status codes and plain-text bodies: 400 invalid image/request, 401 invalid/expired/revoked credential, 403 pet mismatch or browser Origin header, 413 oversize, 429 shared quota, and 502/503 analysis service errors. The response has `Cache-Control: no-store`. API endpoints ignore browser cookies and do not accept browser-origin requests; browser forms still require CSRF protection.

Every accepted image uses the same analysis path as the website, including the pet's last 30 JSON results with UTC timestamps, per-account/global quotas, and encrypted hosted storage. Full results appear under that pet on the website. Other pets' results are not sent as context.

## Operational notes

- TLS certificates are verified. Remote HTTP and all redirects are rejected. Only loopback HTTP is allowed for local developer tests; do not disable certificate verification.
- No automatic upload retry is performed: a timeout can happen after the server has already saved a result. Check the website before resubmitting to avoid duplicate charges/results.
- Revoke credentials from the selected pet's page if a Pi is lost or a token is exposed. Removing a pet invalidates its credentials. Credentials expire automatically and only their hashes are stored by the server.
- The Pi clock and CA certificates must be current. This code was tested against the server with mocked model responses; physical camera capture requires testing on your Pi.
- For a timer or automation, run the script as an unprivileged user with access to the camera and its private token file. `PET_SERVER_URL` can supply the server origin instead of `--server`. Keep credentials out of command-line arguments and logs.
