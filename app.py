from flask import Flask, request, jsonify
import asyncio
import logging
import json
import binascii
import requests
import aiohttp
import like_pb2
import uid_generator_pb2
import visit_count_pb2

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.message import DecodeError
from collections import OrderedDict

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# API keys
VALID_API_KEYS = {"alfa"}

# Usage limits (in-memory; resets when the server restarts)
daily_limit = 20
used_count = 0


def load_tokens(region):
    try:
        if region == "IND":
            filename = "token_ind.json"
        elif region in {"BR", "US", "SAC", "NA"}:
            filename = "token_br.json"
        else:
            filename = "token_bd.json"

        with open(filename, "r", encoding="utf-8") as f:
            tokens = json.load(f)

        if not isinstance(tokens, list) or not tokens:
            app.logger.error("Token file is empty or invalid: %s", filename)
            return None

        return tokens

    except Exception:
        app.logger.exception("Could not load tokens for region %s", region)
        return None


def encrypt_message(plaintext):
    try:
        key = b'Yg&tc%DEuh6%Zc^8'
        iv = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded_message = pad(plaintext, AES.block_size)
        encrypted_message = cipher.encrypt(padded_message)
        return binascii.hexlify(encrypted_message).decode("utf-8")
    except Exception:
        app.logger.exception("Encryption failed")
        return None


def create_protobuf_message(user_id, region):
    try:
        message = like_pb2.like()
        message.uid = int(user_id)
        message.region = region
        return message.SerializeToString()
    except Exception:
        app.logger.exception("Could not create like protobuf")
        return None


def create_protobuf(uid):
    try:
        message = uid_generator_pb2.uid_generator()
        message.saturn_ = int(uid)
        message.garena = 1
        return message.SerializeToString()
    except Exception:
        app.logger.exception("Could not create UID protobuf")
        return None


def enc(uid):
    protobuf_data = create_protobuf(uid)
    if protobuf_data is None:
        return None
    return encrypt_message(protobuf_data)


def get_endpoints(region):
    if region == "IND":
        base = "https://client.ind.freefiremobile.com"
    elif region in {"BR", "US", "SAC", "NA"}:
        base = "https://client.us.freefiremobile.com"
    else:
        base = "https://clientbp.ggpolarbear.com"

    return (
        f"{base}/GetPlayerPersonalShow",
        f"{base}/LikeProfile"
    )


def make_request(encrypted_uid, region, token):
    try:
        info_url, _ = get_endpoints(region)
        payload = bytes.fromhex(encrypted_uid)

        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB55",
        }

        response = requests.post(
            info_url,
            data=payload,
            headers=headers,
            timeout=15
        )

        app.logger.info(
            "Player-info response: HTTP %s, content-type=%s, length=%s",
            response.status_code,
            response.headers.get("Content-Type"),
            len(response.content),
        )

        response.raise_for_status()

        decoded = visit_count_pb2.Info()
        decoded.ParseFromString(response.content)
        return decoded

    except (DecodeError, ValueError):
        app.logger.exception("Player-info protobuf decode failed")
        return None
    except requests.RequestException:
        app.logger.exception("Player-info HTTP request failed")
        return None
    except Exception:
        app.logger.exception("Player-info request failed")
        return None


async def send_request(encrypted_uid, token, url):
    try:
        payload = bytes.fromhex(encrypted_uid)

        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB55",
        }

        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                data=payload,
                headers=headers
            ) as response:
                body = await response.text(errors="replace")

                app.logger.info(
                    "LikeProfile response: HTTP %s, content-type=%s, length=%s",
                    response.status,
                    response.headers.get("Content-Type"),
                    len(body),
                )

                # Do not log response bodies or credentials.
                return body

    except Exception:
        app.logger.exception("LikeProfile request failed")
        return None


async def send_multiple_requests(uid, region, url):
    # Diagnostic mode: one request, not 100 concurrent requests.
    protobuf_message = create_protobuf_message(uid, region)
    if protobuf_message is None:
        return None

    encrypted_uid = encrypt_message(protobuf_message)
    if encrypted_uid is None:
        return None

    tokens = load_tokens(region)
    if not tokens:
        return None

    token = tokens[0].get("token")
    if not token:
        app.logger.error("First token entry has no token field")
        return None

    return await send_request(encrypted_uid, token, url)


@app.route("/like", methods=["GET"])
def handle_requests():
    global used_count

    api_key = request.args.get("key")
    if api_key not in VALID_API_KEYS:
        return jsonify({
            "error": "Invalid or missing API key",
            "status": 3
        }), 401

    uid = request.args.get("uid", "").strip()
    region = request.args.get("region", "").upper().strip()

    if not uid or not region:
        return jsonify({"error": "UID and region are required"}), 400

    if not uid.isdigit():
        return jsonify({"error": "UID must contain digits only"}), 400

    try:
        tokens = load_tokens(region)
        if not tokens:
            return jsonify({"error": "Failed to load tokens"}), 500

        token = tokens[0].get("token")
        if not token:
            return jsonify({"error": "Token missing from token file"}), 500

        encrypted_uid = enc(uid)
        if encrypted_uid is None:
            return jsonify({"error": "UID encryption failed"}), 500

        before = make_request(encrypted_uid, region, token)
        if before is None:
            return jsonify({"error": "Failed to get initial info"}), 502

        before_like = before.AccountInfo.Likes
        _, like_url = get_endpoints(region)

        # Diagnostic test: send one request and inspect its HTTP status in logs.
        try:
            asyncio.run(send_multiple_requests(uid, region, like_url))
        except Exception:
            app.logger.exception("Like operation failed")

        after = make_request(encrypted_uid, region, token)
        if after is None:
            return jsonify({"error": "Failed to get final info"}), 502

        after_like = after.AccountInfo.Likes
        like_given = after_like - before_like
        status = 1 if like_given > 0 else 2

        if status == 1:
            used_count += 1

        remaining = max(daily_limit - used_count, 0)

        result = OrderedDict([
            ("LikesGivenByAPI", like_given),
            ("LikesafterCommand", after_like),
            ("LikesbeforeCommand", before_like),
            ("PlayerNickname", after.AccountInfo.PlayerNickname),
            ("Level", after.AccountInfo.Levels),
            ("Region", after.AccountInfo.PlayerRegion),
            ("UID", after.AccountInfo.UID),
            ("status", status),
            ("daily_limit", daily_limit),
            ("used", used_count),
            ("remaining", remaining),
        ])

        return app.response_class(
            response=json.dumps(result, separators=(",", ":")),
            status=200,
            mimetype="application/json",
        )

    except Exception:
        app.logger.exception("Error handling /like request")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/remain", methods=["GET"])
def remain_info():
    remaining = max(daily_limit - used_count, 0)
    return jsonify({
        "daily_limit": daily_limit,
        "remaining": remaining,
        "used": used_count,
        "reset_info": "4:00 AM IST",
    })


if __name__ == "__main__":
    app.run(debug=False, use_reloader=False)
