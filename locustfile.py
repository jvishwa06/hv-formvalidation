import os
from locust import HttpUser, task, between
from locust.exception import RescheduleTask

class FormValidatorUser(HttpUser):
    wait_time = between(1, 3)
    
    def on_start(self):
        self.pdf_file_path = "sample3.pdf"
        if not os.path.exists(self.pdf_file_path):
            print(f"Error: {self.pdf_file_path} not found!")
            raise RescheduleTask()
    
    @task
    def validate_application(self):
        try:
            with open(self.pdf_file_path, 'rb') as pdf_file:
                files = {'file': ('sample3.pdf', pdf_file, 'application/pdf')}
                
                response = self.client.post("/prod/v1/validate-application",files=files,name="validate-application")
                
                if response.status_code != 200:
                    print(f"Request failed with status {response.status_code}: {response.text}")
                
        except Exception as e:
            print(f"Error during request: {str(e)}")
            self.client.post("/prod/v1/validate-application", 
                           name="validate-application", 
                           catch_response=True).failure(f"Exception: {str(e)}")



# locust -f locustfile.py --host=https://85vh3ypjr1.execute-api.us-east-1.amazonaws.com