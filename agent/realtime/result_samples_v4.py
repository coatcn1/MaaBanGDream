"""Hard-result glyphs captured from the current 1280x720 result panel.

The payload is seven thresholded, right-padded 32x60 crops in
``ResultParser.FIELDS`` order.  In this rendering, the lower-left segment of
the final ``9`` is faint enough that the older sample set classified it as
``0``; both GREAT and MISS therefore lost nine counts during calibration.
"""

RESULT_CROPS_V4_ZLIB_BASE64 = (
    "eNrtWsuC4zAI0///tPawjwSQwG62M7M75tKkqRLHBgSqgWP/v5EMJ7x/cTtOV/KP"
    "+cduV82l21fx+q9fpKME/nnI64PX4Mpdypiuz/h8yBHEF7wOJmydAcQHI4+9wf"
    "ZrwfBSecyoA1DY+33z0zIWEsslrHpuWV6Iu4gpku5qndr4pIiD3wuKZjXrywvfg"
    "D6IU976ZMXq4BUxELA0cdTEYI8dAq3HhjW0MQqDBYzXBQfACD323WiuI7PmsuAm"
    "yWUmV13nNBx5jxYdOwLLe+iLDyZySikFFaTIiXFsisrQYaGeJ7F1zBmLHsvynl"
    "zCoizRK1jqOmYaM8T6ztic30X9tIZl/dWAvTNqV4FYv0oLPGDjqkSfLAWHjCMOc"
    "fQofh/ljY18dZjs2PtYsXzTnZmw6eNopVdU19s6VZTADQ+mfmvMV/VsnQcRG7B"
    "dLAylrvBCxu5wCjSl4iOxD8as6hwszrP1iXF95zoHGhtZESs8aHpFtDwoHbnOl"
    "YvBpV4R7MrrPvZj21bPwKbfO3bsK7Jqd1YlCqILo4FC+YBCd9MGDNTzLmoVr1W"
    "zNfo12C49wwjIy/SbFuEVKtvF8gl1b9Eg9cUXKNQomvtYLlModijU0S+lVD+Xk"
    "+sUWmOQnkLn8lnKoqtl+zNZ9LDvM/nR2Buxx1oX/ko6sCDoBezRgf+6DsyP14H"
    "5SAfm5+rAhXVSxTPowCsacqoCv48OzKMDHzt2TOcGIzvY/mLWGUR56U8aZcEQo"
    "0j1bq9IqDzanq6m3VxEKKzZJFRIxisLZWYzFhOWT7F4CSv3cb0fy6dYvAErSxX"
    "oTVca63xSt+jBodn4ZvXOUoDonl62NDkizd94Zo+nUWugZ8fImX0yG9PbsWOfo"
    "9qgq+vRF/3yb6817c92HyUT2Q2XpekZRPu622YgYNFy+SZPJ+gJOxJhGLPLai0B"
    "11txkQhV/bGOrcPYGHPZjcy9ucp1H/o1yhOtt+xK6SBi9Z8x1a9UZaW3bMt7UP"
    "lxisGyz9vEkS1r0YsFWutX3K7zxq7SyzYH/Wv2A+g1Nqs="
)

RESULT_CROPS_V4_LABELS = (
    (0, 3, 1, 2),
    (0, 0, 8, 9),
    (0, 0, 0, 8),
    (0, 0, 0, 5),
    (0, 0, 8, 9),
    (0, 0, 7, 0),
    (0, 0, 3, 2),
)
