#!/usr/bin/env python3

import pandas as pd
import seaborn as sns
import numpy as np
from matplotlib import pyplot as plt

def SMA(data, period=1, column='WEOUT'):
    return data[column].rolling(window=period).mean()

results = pd.read_csv("test.csv", names=["WEOUT", "REOUT", "VREFINT", "T", "H", "C", "D"])
results["PR"] = results["REOUT"] - results["VREFINT"]
results["WEOUT_SMA"] = SMA(results, column='WEOUT')
results["REOUT_SMA"] = SMA(results, column='REOUT')
line = sns.scatterplot(x=results["REOUT_SMA"], y=results["WEOUT_SMA"])
# line.set(xlim=[-0.75, 0.75])
line.set(xlabel="Volage / V")
line.set(ylabel="Current / mA")
fig = line.get_figure()
ax = fig.axes[0]
# ax.set(ylim=[1.094+0.000650, 1.094+0.000750])
fig.savefig("out.png")
plt.clf()
