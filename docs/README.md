# Coffee Data Tracker

## Overview

The Coffee Data Tracker project is an initiative to collect data on coffee brewing and analyze this data on Google Cloud Platform (GCP). This project explores various methods of data collection and integration into GCP, emphasizing learning and skill development in cloud technologies.

### Project Journey

The journey began with an ambition to integrate a custom form from a Squarespace website directly into GCP. The objective was to capture specific coffee brewing metrics and store them for analysis. Various approaches were considered and tested, leading to valuable insights and pivot decisions:

- **Initial Approach**: The plan was to use JavaScript to capture form data and send it directly to GCP. However, this method faced limitations due to the need for an upgraded Squarespace account to utilize integrations like Zapier effectively.
  
- **Squarespace Form Creation**: A form was successfully created and deployed on [jtouley.com/coffee-tracker](https://www.jtouley.com/coffee-tracker). Although direct retrieval of payloads through JavaScript proved challenging, this step was crucial in understanding the available data integration options.
  
- **Pivoting to Data/Storage Connectors**:
    - **Zapier Integration**: Initially considered the preferred method for its direct BigQuery integration capabilities, it was eventually set aside due to the need for account upgrades on both Squarespace and potentially Zapier.
    - **Google Sheets Integration**: Emerged as the chosen solution due to its simplicity and effectiveness. The integration process was straightforward, and initial tests confirmed its viability for the project's goals.

## Infrastructure as Code (IaC) and CLI in GCP

Recognizing the importance of IaC for modern cloud projects, this project aims to include IaC methodologies for setting up and managing the GCP infrastructure. It provides an opportunity to showcase skills in using GCP's Cloud Functions, BigQuery, and Cloud Storage, along with automating deployment and data processing workflows.

- **IaC Approach**: Detailed instructions and code samples for deploying the necessary GCP resources using Terraform or Google Cloud Deployment Manager will be included.
  
- **CLI Methods**: For those new to IaC or preferring hands-on learning, CLI commands for setting up resources in GCP will also be provided, offering insights into managing cloud resources through Google Cloud SDK.

## Getting Started

(Here, you would include instructions on how to set up the project, both using the web portal and IaC/CLI methods. This section will guide users on cloning the repo, installing necessary tools, and deploying the infrastructure required for the project.)

## Contributing

We welcome contributions and suggestions! Feel free to fork the repository, make changes, and submit pull requests. For major changes, please open an issue first to discuss what you would like to change.
