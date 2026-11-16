export default function BrandMark({ className = "size-9" }) {
  const authMark = className.includes("bg-[#e9cdb3]");
  return <svg className={`shrink-0 text-[var(--color-brand)] ${className} ${authMark ? "!bg-transparent !text-[#e9cdb3]" : ""}`} viewBox="0 0 298 293" fill="currentColor" aria-hidden="true">
    <path d="M4 84C1.5 84 0 86.4 0 90.2v120.5c0 5.7 3.1 9.8 8.2 12.6l29.9 13.8c2.9 1.4 4.9-.2 4.9-3.8V107c0-5-2.5-8.6-7.1-10.8L9.2 84.4C7.3 83.5 5.6 84 4 84Z" />
    <path d="M67.8 48C64.8 48 63 50.5 63 54.9v185.8c0 6.1 3.2 10.3 8.4 13.1l39.6 17.4c4.1 1.9 7-.2 7-5.1V76.1c0-5.5-2.7-9.4-7.7-11.7L74.1 48.7c-2.1-.9-4.3-.7-6.3-.7Z" />
    <path fillRule="evenodd" d="M149.2 0c-3.9 0-7 1.9-9.1 5.2-1.5 2.5-2.1 5.3-2.1 8.5v263.7c0 6.4 3.4 11.1 8.7 13.8 4.4 2.3 9.2 2.4 14-.3l127.7-55.6c6.4-2.9 9.6-7.9 9.6-15V78c0-6.7-3.4-11.7-9.5-14.6L160.4 2.1C156.5.6 152.6 0 149.2 0Zm72.3 103.2a54.8 54.8 0 1 0 0 109.6 54.8 54.8 0 0 0 0-109.6Z" />
    <path d="m195.2 159.6 18.8 18.5 33.7-39.4" fill="none" stroke="currentColor" strokeWidth="13.8" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}
