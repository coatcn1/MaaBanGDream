"""Hard-result glyphs covering the current ``8``/``3`` rendering ambiguity.

The payload is seven thresholded, right-padded 32x60 crops in
``ResultParser.FIELDS`` order.  The GREAT value is ``0081``; without this
sample, the soft lower-left segment of the ``8`` was classified as ``3``.
"""

RESULT_CROPS_V5_ZLIB_BASE64 = (
    "eNrtW9tWwzAM0///tHjgnC225UvaAQOSF9aL1sy1LVsJwBl/fZAMJ7h8pLmw3kx/"
    "YrmfXK/SHvrLGZbmgGbCK/h5+fODwbpvdr+ZFmtBxhoPjJ8m7M1XsRhiWWOd3Y"
    "PNCI0N78hieRcLXJmz+6EWS/F+Pfb53QJr3N3YmXFYLF6ODe7ubKbmDP98BGdc"
    "bWRspWLQYlUMMo196NiPUBe87oTOE00G08lsAD3jn1EdM2cJl2u+Er4fIwsyAG"
    "LIwaf9JHhEGIksoOhMhqB9vEvYGGAfd6dYCCxSrKPgYKsWm9uZJRYqvQ2xd55"
    "Lw2Nbv1dywRUsFbZ8v5hgK7+aYe0fNr5RxVHCxa+J3628sUVIDPnqpPAzvoIUi"
    "xNZk6aKsyKKnDd3DWHSPqJoCFsWHDSEOnmpzNJkSZhWiLtY0QGt00GfnX8ci7"
    "efc2/n2fvtsdDYxq8qt86w+rwEOa0ij0FMBB2wYMFBU1cdQUtMZ5zxvqRaHQU"
    "GbcKooVDeoNBraSNCc95FrOGTRmNEv6MmRfQduntvaUFVKlM6UrbaorJCy+2fm"
    "+qiLda3lVMKTRu5XSzHFApLYftlHTMKHZSTvaaaxyBzTbUvnweaal62c6Splj3"
    "pye+XxcdkfCH2jB1NuFRzukCaBHDewxLielw5SvWovl/gjf6XN/vfq8Twkl6S3"
    "4vFbfK+iIXQOXcI2FcA0wJLLQGuFU9T2OFi/xs14XI1pSlk6/htFzXH/e+txH"
    "1S/hlnnCEaJ6szMO34OZQZZKtCzLqWFIt094pcpFu3wjRyA/N2RbNRpizYUq/k"
    "vaQKf5gdI6zUN6dYSb9TLGNh22MVu6HHwi/+OuZGX18Eudo5/hRLiS0UKejl3"
    "+CLYsEazLBdLFg7q0Kl0eb1ivWo6ElXu/P30e+S2VuIPFXOGe+i2mj9a9ICTPT"
    "4LgrjIcp9w/n+aY5Y1MkEz0OlFajpUMyuYZUK2zASoHs9uWk8k5wvYqlsNcJm"
    "1VmPTbN9j2Va6XV2RrJoG94v0ooj1KmdX4nKLm4v7PzZhcISpXJDti5cpbIyUP"
    "eSKlH+I4WQenflgmRN8Ndm3w/uR7sY"
)

RESULT_CROPS_V5_LABELS = (
    (0, 4, 0, 8),
    (0, 0, 8, 1),
    (0, 0, 0, 0),
    (0, 0, 0, 5),
    (0, 0, 0, 9),
    (0, 0, 5, 7),
    (0, 0, 2, 9),
)
