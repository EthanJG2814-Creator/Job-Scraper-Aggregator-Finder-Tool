const countryNames = typeof Intl.DisplayNames === "function"
  ? new Intl.DisplayNames(["en"], { type: "region" }) : null;

function countryName(code) {
  try { return countryNames?.of(code) || code; } catch { return code; }
}

class LocationPicker {
  constructor() {
    this.options = [];
    this.selected = new Map();
    this.container = document.getElementById("location-options");
    this.summary = document.getElementById("location-summary");
  }

  checkbox(label, checked, onChange) {
    const wrapper = document.createElement("label");
    wrapper.className = "location-choice";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    input.addEventListener("change", () => onChange(input.checked));
    const text = document.createElement("span");
    text.textContent = label;
    wrapper.append(input, text);
    return wrapper;
  }

  cityChoices(container, cities, chosen) {
    if (!cities.length) return;
    const list = document.createElement("div");
    list.className = "location-children city-choices";
    const heading = document.createElement("h3");
    heading.className = "location-section-heading";
    heading.textContent = "Select city";
    const hint = document.createElement("p");
    hint.className = "location-help";
    hint.textContent = "Select cities, or leave all unchecked to include all.";
    list.append(heading, hint);
    for (const city of cities) {
      list.append(this.checkbox(city, chosen.has(city), (checked) => {
        if (checked) chosen.add(city); else chosen.delete(city);
      }));
    }
    container.append(list);
  }

  render() {
    const menu = this.container.closest(".location-menu");
    const scrollTop = menu.scrollTop;
    this.container.replaceChildren();
    this.summary.textContent = this.selected.size
      ? `${this.selected.size} ${this.selected.size === 1 ? "country" : "countries"} selected`
      : "All locations";
    if (!this.options.length) {
      this.container.textContent = "No parsed country locations are available yet.";
      return;
    }
    for (const country of this.options) {
      const section = document.createElement("div");
      const selection = this.selected.get(country.code);
      section.append(this.checkbox(`${countryName(country.code)} (${country.code})`, !!selection, (checked) => {
        if (checked) this.selected.set(country.code, { states: new Map(), cities: new Set() });
        else this.selected.delete(country.code);
        this.render();
      }));
      if (selection) {
        const stateEntries = Object.entries(country.states);
        if (stateEntries.length) {
          const states = document.createElement("details");
          states.className = "location-children";
          states.open = true;
          const summary = document.createElement("summary");
          summary.textContent = "Select state / province (optional)";
          const hint = document.createElement("p");
          hint.className = "location-help";
          hint.textContent = "Leave all unchecked to include the whole country.";
          states.append(summary, hint);
          for (const [state, cities] of stateEntries) {
            const group = document.createElement("div");
            const selectedCities = selection.states.get(state);
            const stateName = country.state_names[state];
            const stateLabel = stateName && stateName !== state ? `${stateName} (${state})` : state;
            group.append(this.checkbox(stateLabel, !!selectedCities, (checked) => {
              if (checked) selection.states.set(state, new Set());
              else selection.states.delete(state);
              this.render();
            }));
            if (selectedCities) this.cityChoices(group, cities, selectedCities);
            states.append(group);
          }
          section.append(states);
        } else {
          this.cityChoices(section, country.cities, selection.cities);
        }
      }
      this.container.append(section);
    }
    menu.scrollTop = scrollTop;
  }

  async load() {
    try {
      const response = await fetch("/api/location-options");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      if (!data.available) {
        this.container.textContent = "Location data is not available in this database.";
        return;
      }
      this.options = data.countries.sort((a, b) => countryName(a.code).localeCompare(countryName(b.code)));
      this.render();
    } catch (error) {
      this.container.textContent = `Unable to load locations: ${error.message}`;
    }
  }

  value() {
    return [...this.selected].map(([country, selection]) => ({
      country,
      states: Object.fromEntries([...selection.states].map(([state, cities]) => [state, [...cities]])),
      cities: [...selection.cities],
    }));
  }

  clear() {
    this.selected.clear();
    this.render();
  }
}
