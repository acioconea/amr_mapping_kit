import math
import statistics
from typing import List, Dict, Any


def calculate_mkt(temperatures_celsius: List[float]) -> float:
    """
    Calculează Temperatura Medie Cinetică (MKT) folosind ecuația Arrhenius.
    O pondere mai mare este acordată temperaturilor ridicate.
    """
    if not temperatures_celsius:
        return 0.0

    delta_H = 83.144  # Energia de activare (kJ/mol) pt farmaceutice
    R = 0.0083144  # Constanta universală a gazelor (kJ/mol·K)
    activation_ratio = delta_H / R  # aprox. 10000 K

    sum_exp = 0.0
    for temp_c in temperatures_celsius:
        temp_k = temp_c + 273.15  # Transformare în Kelvin
        sum_exp += math.exp(-activation_ratio / temp_k)

    avg_exp = sum_exp / len(temperatures_celsius)

    if avg_exp == 0:
        return 0.0

    mkt_k = activation_ratio / (-math.log(avg_exp))
    mkt_c = mkt_k - 273.15
    return round(mkt_c, 2)


def generate_gdp_report(mission_id: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Procesează toate punctele din misiune și generează raportul GDP."""
    all_temps = []

    # Inițializăm căutarea extremelor
    hot_spot = {"temp": -999.0, "x": None, "y": None, "z": None}
    cold_spot = {"temp": 999.0, "x": None, "y": None, "z": None}

    for record in records:
        x = record.get("x")
        y = record.get("y")
        profile = record.get("vertical_profile", [])

        for point in profile:
            temp = point.get("temperature")
            z = point.get("z_level")

            if temp is not None:
                all_temps.append(temp)

                # Căutăm Hot Spot-ul global
                if temp > hot_spot["temp"]:
                    hot_spot = {"temp": temp, "x": x, "y": y, "z": z}

                # Căutăm Cold Spot-ul global
                if temp < cold_spot["temp"]:
                    cold_spot = {"temp": temp, "x": x, "y": y, "z": z}

    if not all_temps:
        raise ValueError("Misiunea nu conține date de temperatură valide.")

    mean_temp = statistics.mean(all_temps)
    std_dev = statistics.stdev(all_temps) if len(all_temps) > 1 else 0.0
    mkt = calculate_mkt(all_temps)

    # Verificare limită ambientală GDP (15 - 25 °C)
    is_compliant = 15.0 <= mkt <= 25.0

    return {
        "mission_id": mission_id,
        "total_points": len(all_temps),
        "compliance": "CONFORM (15-25°C)" if is_compliant else "NECONFORM",
        "compliance_color": "success" if is_compliant else "danger",
        "stats": {
            "min_temp": round(min(all_temps), 2),
            "max_temp": round(max(all_temps), 2),
            "mean_temp": round(mean_temp, 2),
            "std_dev": round(std_dev, 3),
            "mkt": mkt
        },
        "zones": {
            "hot_spot": hot_spot,
            "cold_spot": cold_spot
        }
    }