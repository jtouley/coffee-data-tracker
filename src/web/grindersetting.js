document.addEventListener("DOMContentLoaded", function () {
    const grinderSettingSelect = document.getElementById("grinder_setting");

    // Function to generate a single option element
    function createOption(value, text) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        return option;
    }

    // Starting and ending values for the grinder setting range
    const startSetting = 0;  // Adjust as needed (assuming grinder settings start at 1)
    const endSetting = 100;    // Adjust as needed (assuming grinder settings end at 6)

    // Loop to generate options with 0.25 increments
    for (let setting = startSetting; setting <= endSetting; setting += 0.25) {
        const formattedSetting = setting.toFixed(2); // Format to two decimal places
        const option = createOption(formattedSetting, formattedSetting);
        grinderSettingSelect.appendChild(option);
    }

    // Set the default selected value to 15
    grinderSettingSelect.value = 15.00;

    // Remove the placeholder option after adding all valid options
    grinderSettingSelect.removeChild(grinderSettingSelect.firstChild);
});