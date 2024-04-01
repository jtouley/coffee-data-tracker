import functions_framework
from google.cloud import storage
from google.cloud import pubsub_v1
import datetime
import json
import os

# Environment variables
PROJECT_ID = os.getenv('GCP_PROJECT_ID')
TOPIC_NAME = os.getenv('PUBSUB_TOPIC_NAME')
BUCKET_NAME = os.getenv('GCS_BUCKET')

publisher = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_NAME)

@functions_framework.http
def process_coffee_data(request):
    # Set CORS headers for the preflight request
    if request.method == "OPTIONS":
        # Allows POST requests from any origin with the Content-Type header and caches preflight response for 3600s
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)

    # Set CORS headers for the main request
    headers = {"Access-Control-Allow-Origin": "*"}

    # Process the coffee data submission
    request_json = request.get_json()
    if request_json:
        # Publish to Pub/Sub
        future = publisher.publish(topic_path, json.dumps(request_json).encode("utf-8"))
        future.result()

        # Store in Cloud Storage
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"coffee-data-{datetime.datetime.now(datetime.timezone.utc).isoformat()}.json")
        blob.upload_from_string(json.dumps(request_json), content_type='application/json')

        # Include CORS headers in the response
        return ("Success", 200, headers)
    else:
        # Include CORS headers even in the error response
        return ("Bad Request", 400, headers)