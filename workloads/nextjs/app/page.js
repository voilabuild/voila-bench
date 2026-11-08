export const dynamic = "force-dynamic"; // render per request, so the check hits the server, not a static file

export default function Page() {
  return <main><h1>voila bench</h1><p>rendered at {new Date().toISOString()}</p></main>;
}
