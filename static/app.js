  const uploadForm = document.getElementById('upload-form');
  if (uploadForm) uploadForm.addEventListener('submit', function(event) {
    const file = document.getElementById('image').files[0];
    if (file && file.size > 10 * 1024 * 1024) {
      event.preventDefault();
      document.getElementById('status').textContent = 'Choose an image smaller than 10 MB.';
      return;
    }
    document.getElementById('submit').disabled = true;
    document.getElementById('status').textContent = 'Analyzing your image. This may take up to 90 seconds...';
  });

document.querySelectorAll('[data-confirm]').forEach(function(form) {
  form.addEventListener('submit', function(event) {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
