document.addEventListener("DOMContentLoaded", function () {
    const form = document.querySelector("#coffeeReviewForm");
    form.addEventListener("submit", function (e) {
        e.preventDefault(); // Prevent the default form submission

        // Initialize an empty object
        let formData = {};

        // Using FormData to capture all input values including radio buttons
        const data = new FormData(form);
        for (const [key, value] of data.entries()) {
            formData[key] = value;
        }

        // Add created_at and received_at fields
        const now = new Date().toISOString();
        formData.created_at = now;
        formData.received_at = now;

        // Log formData to check if the values are correct
        console.log(formData);

        // Replace YOUR_CLOUD_FUNCTION_URL with the actual URL of your Google Cloud Function
        fetch("YOUR_CLOUD_FUNCTION_URL", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(formData)
        })
            .then(response => response.json())
            .then(data => {
                console.log("Success:", data);
                // Handle success here
            })
            .catch((error) => {
                console.error("Error:", error);
                // Handle errors here
            });
    });
});