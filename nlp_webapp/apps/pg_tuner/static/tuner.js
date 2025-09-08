document.addEventListener('htmx:beforeRequest', (e)=>{
  const form = e.target.closest('.tune-form');
  if(form) form.classList.add('htmx-request');
});
document.addEventListener('htmx:afterOnLoad', (e)=>{
  document.querySelectorAll('.tune-form').forEach(f=>f.classList.remove('htmx-request'));
});
