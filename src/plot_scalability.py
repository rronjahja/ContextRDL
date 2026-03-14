import pandas as pd
import matplotlib.pyplot as plt


df = pd.read_csv("results/scalability.csv")

plt.figure(figsize=(6,4))

plt.plot(df["actions"], df["time_seconds"], marker="o")

plt.xlabel("|Act_t| (number of concurrent rule instances)")
plt.ylabel("Resolver time (seconds)")
plt.title("Resolver scalability")

plt.grid(True)

plt.tight_layout()

plt.savefig("results/scalability_plot.png")

plt.show()