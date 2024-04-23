document.addEventListener("DOMContentLoaded", function () {
    const form = document.querySelector("#coffeeReviewForm");

    // Function to process form data
    function processFormData(formData) {
        // Convert empty 'notes' to null
        if (formData.notes === "") {
            formData.notes = null;
        }
        
        // Convert specific fields to correct types
        formData.grinder_setting = parseFloat(formData.grinder_setting);  // Convert to float
        formData.coffee_dose = parseFloat(formData.coffee_dose);           // Convert to float
        formData.coffee_yield = parseFloat(formData.coffee_yield);         // Convert to float
        formData.shot_duration = parseInt(formData.shot_duration);         // Convert to int
        formData.coffee_rating = parseInt(formData.coffee_rating);         // Convert to int
        formData.is_latte = (formData.is_latte === "true");                // Convert to boolean
        
        return formData;
    }

    form.addEventListener("submit", function (e) {
        e.preventDefault(); // Prevent the default form submission

        // Collect form data
        let formData = {};
        const data = new FormData(form);
        for (const [key, value] of data.entries()) {
            formData[key] = value;
        }

        // Process form data for null handling and data type conversion
        formData = processFormData(formData);

        // Add timestamps
        const now = new Date().toISOString();
        formData.created_at = now;
        formData.received_at = now;

        // Check processed data
        console.log(formData);  // Useful for debugging

        // Submit the form data
        fetch("YOUR_CLOUD_FUNCTION_URL", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(formData),
        })
        .then((response) => response.json())
        .then((data) => {
            console.log("Success:", data);
            // Handle successful response
        })
        .catch((error) => {
            console.error("Error:", error);
            // Handle errors
        });
    });
});
