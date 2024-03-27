document.addEventListener("DOMContentLoaded", function() {
    const form = document.querySelector("form");
    form.addEventListener("submit", function(e) {
        e.preventDefault(); // Prevent the default form submission

        // Capture form data
        const formData = {
            coffee_roaster: form.querySelector('[name="coffee_roaster"]').value,
            coffee_roast: form.querySelector('[name="coffee_roast"]').value,
            coffee_origin: form.querySelector('[name="coffee_origin"]').value,
            grinder_setting: form.querySelector('[name="grinder_setting"]').value,
            coffee_dose: form.querySelector('[name="coffee_dose"]').value,
            coffee_yield: form.querySelector('[name="coffee_yield"]').value,
            shot_duration: form.querySelector('[name="shot_duration"]').value,
            coffee_rating: form.querySelector('[name="coffee_rating"]').value,
            notes: form.querySelector('[name="notes"]').value
        };

        // Add created_at and received_at fields
        const now = new Date().toISOString();
        formData.created_at = now;
        formData.received_at = now;

        // Replace YOUR_CLOUD_FUNCTION_URL with the actual URL of your Google Cloud Function
        fetch("YOUR_CLOUD_FUNCTION_URL", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify(formData),
        })
        .then(response => response.json())
        .then(data => {
            console.log("Success:", data);
            // You can redirect the user or display a success message here
        })
        .catch((error) => {
            console.error("Error:", error);
            // Handle errors, such as by displaying an error message to the user
        });
    });
});
