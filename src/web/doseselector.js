document.addEventListener("DOMContentLoaded", function () {
    const coffeeDoseSelect = document.getElementById("coffee_dose");

    // Function to generate a single option element
    function createOption(value, text) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        return option;
    }

    // Starting and ending values for the coffee dose range
    const startDose = 17.5;
    const endDose = 22.5;

    // Loop to generate options with 0.1 increments
    for (let dose = startDose; dose <= endDose; dose += 0.1) {
        const formattedDose = dose.toFixed(1); // Format to one decimal place
        const option = createOption(formattedDose, formattedDose);
        coffeeDoseSelect.appendChild(option);
    }

    // Remove the placeholder option after adding all valid options
    coffeeDoseSelect.removeChild(coffeeDoseSelect.firstChild);
});