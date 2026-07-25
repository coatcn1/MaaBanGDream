"""Additional result glyphs captured from the 1280x720 result panel.

The payload is seven thresholded, right-padded 32x60 crops in ResultParser.FIELDS
order.  Keeping the source crops (instead of only normalised glyphs) makes the
real rendering variant reusable as a regression fixture.
"""

RESULT_CROPS_V2_ZLIB_BASE64 = (
    "eNrtmsuW4zAIRPn/n66ZRT8sqJJAdKZzzsDGkZWbyLKgQLbZ2H9gAFTrs4GHrd9dzjwbn58BCvsz9LNmv/9g6SWfGfpxXLrJ5RlnLcO69oMFH+eOZXeDzXlAOywKLLmCf8GGG2Y9Fq9nI7quySJr3yvwipU++GAZGpZtiSUrz/iYM+GL3f719NiYXx+m/IG3jEsB96ScXzE1+zg6caADipFjOSS0bf17i8J4Yr++LVijrEn2MFcYNs1aj0WbZeuqxqa1LeFHZ/bafwtxAzlFU7n4xO+xFxR/2GdmqkKQJZ32Id/ne0N/bIGxu4MvFZ9XweONC10srpxiZCwVK+zjwthYbae9b8La24/5PM+5+3tmjbOHdbVb1orl5ykEL5B7DRQeiV01KDqxSwqZ0m22RsbG3r3OjC2o1FH4EVJ+1PHfRtyQ8aqsoXdxsh+fR39fN+bOPCfuL/b51U1e18knW3lsJ39u5e29emEsPW/SXsiOJaa2spcj0uWk/+oaFuyx59rp18LikMl64RfqX2vUv1+DRV2P4jwXWPslltzfsv5e7jNYo/6V7GFdafa8npGrf6Hz2F1CvVFySPFO1L+9yD0xf2xsjGwlkI0z9bbIYZ9BPLdlZYx4iUSVH0HLfVSHxbdBmIgouTAahAWrfmu5/p3ykeebLtGzWxZFFhesPeTMrth1N6g2ZnixLrIPIa2wCGxhbeRYfvRVW9kXWj5oOPg+Mr4fg4p8deBHNgwm1Rl7xx0crZlUQv2Ozb7y0MXGdyXmyn/wkEfRk5SyUV9IqC21+067ggzarSy4dKMmgz7CVlk0WbtjUWbdttNPsLX7y+Qkriu5G6HY03qOr52HPteW7zHn/FdL5PER4KjnX/sDRvL3vw=="
)

# Labels for the four cells in each crop, in parser field order.
RESULT_CROPS_V2_LABELS = (
    (0, 1, 1, 7),
    (0, 0, 1, 1),
    (0, 0, 0, 0),
    (0, 0, 0, 0),
    (0, 0, 0, 8),
    (0, 0, 0, 4),
    (0, 0, 0, 7),
)
