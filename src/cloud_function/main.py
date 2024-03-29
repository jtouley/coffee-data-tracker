from google.cloud import storage
import datetime
import json

def process_coffee_data(request):
    request_json = request.get_json()
    if request_json:
        client = storage.Client()
        # Make sure to replace 'YOUR_COFFEE_DATA_BUCKET_NAME' with your actual bucket name
        bucket = client.bucket('YOUR_COFFEE_DATA_BUCKET_NAME')
        # Corrected datetime usage here
        blob = bucket.blob(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()}.json")
        blob.upload_from_string(json.dumps(request_json), content_type='application/json')
        return 'Success', 200
    else:
        return 'Bad Request', 400
