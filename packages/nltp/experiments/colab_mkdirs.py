"""Creates the remote layout used by scripts/fair_telemetry_benchmark.py on a Colab VM."""
import os

for d in ("/content/necklink/necklink", "/content/necklink/scripts", "/content/necklink/results",
          "/content/aqms/sensor raw data", "/content/aqms/Handover_Terra_Team/03_Templates_and_test_values"):
    os.makedirs(d, exist_ok=True)
print("dirs ok")
