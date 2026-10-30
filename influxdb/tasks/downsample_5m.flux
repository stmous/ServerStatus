// Downsample the raw bucket into 5-minute mean/min/max.
//
// Placeholders (@...@) are filled in by influxdb/setup.sh.
//
// Each run recomputes the last two complete 5-minute windows, so a point
// that arrives late (e.g. flushed from the collector's local buffer after an
// InfluxDB restart) is still included. Rewriting a window is idempotent:
// same series and timestamp overwrite the previous value.
//
// Output: measurement "environment", same tags plus agg=mean|min|max, fields
// temperature/humidity/dew_point. Timestamps mark the END of each window.

import "date"

option task = {name: "serverroom-downsample-5m", every: 5m, offset: 1m}

stop = date.truncate(t: now(), unit: 5m)
start = date.sub(d: 10m, from: stop)

data =
    from(bucket: "@RAW_BUCKET@")
        |> range(start: start, stop: stop)
        |> filter(fn: (r) => r._measurement == "environment")

data
    |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
    |> set(key: "agg", value: "mean")
    |> to(bucket: "@DS_BUCKET@", org: "@ORG@")

data
    |> aggregateWindow(every: 5m, fn: min, createEmpty: false)
    |> set(key: "agg", value: "min")
    |> to(bucket: "@DS_BUCKET@", org: "@ORG@")

data
    |> aggregateWindow(every: 5m, fn: max, createEmpty: false)
    |> set(key: "agg", value: "max")
    |> to(bucket: "@DS_BUCKET@", org: "@ORG@")
