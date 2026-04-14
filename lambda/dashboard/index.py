import json
import os
import boto3

ec2 = boto3.client("ec2", region_name=os.environ["AWS_REGION"])
s3 = boto3.client("s3", region_name=os.environ["AWS_REGION"])
INSTANCE_ID = os.environ["EC2_INSTANCE_ID"]
BUCKET = os.environ["CONFIG_BUCKET"]
APPS_KEY = os.environ.get("APPS_CONFIG_KEY", "config/dashboard-apps.json")


def handler(event, context):
    raw_path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    if raw_path == "/api/ec2/status":
        return api_response(get_status())

    if raw_path == "/api/ec2/start" and method == "POST":
        ec2.start_instances(InstanceIds=[INSTANCE_ID])
        return api_response({"action": "starting"})

    if raw_path == "/api/ec2/stop" and method == "POST":
        ec2.stop_instances(InstanceIds=[INSTANCE_ID])
        return api_response({"action": "stopping"})

    if raw_path == "/api/apps":
        return api_response(get_apps())

    return serve_html()


def get_apps():
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=APPS_KEY)
        return json.loads(obj["Body"].read())
    except Exception:
        return []


def get_status():
    resp = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
    inst = resp["Reservations"][0]["Instances"][0]
    return {
        "state": inst["State"]["Name"],
        "publicIp": inst.get("PublicIpAddress", None),
        "instanceType": inst.get("InstanceType"),
    }


def api_response(body, code=200):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def serve_html():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path) as f:
        html = f.read()
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }
