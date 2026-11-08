export function sumColumn(csv, column) {
  const [header, ...rows] = csv.trim().split("\n").map((l) => l.split(","));
  const i = header.indexOf(column);
  if (i < 0) throw new Error(`no column ${column}`);
  return rows.reduce((acc, r) => acc + Number(r[i]), 0);
}
