# -*- coding: utf-8 -*-
from PIL import Image
import numpy as np

im = Image.open("resources/icon.png").convert("RGBA").resize((60, 60))
arr = np.array(im)
a = arr[:, :, 3]
r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
for y in range(0, 60, 2):
    row = ""
    for x in range(60):
        if a[y, x] < 32:
            row += " "
        elif r[y, x] >= 190 and g[y, x] >= 190 and b[y, x] >= 190:
            row += "@"
        else:
            row += "#"
    print(row)
