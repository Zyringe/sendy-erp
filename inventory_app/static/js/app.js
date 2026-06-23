// Auto-dismiss flash alerts after 4 seconds
document.addEventListener('DOMContentLoaded', function () {
  setTimeout(function () {
    document.querySelectorAll('.alert.alert-dismissible').forEach(function (el) {
      var bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      bsAlert.close();
    });
  }, 4000);
});

// Persist sidebar scroll position across page navigation
(function () {
  var KEY = 'sendy.sidebarScroll';
  var nav = document.querySelector('.sidebar-nav');
  if (!nav) return;

  var saved = sessionStorage.getItem(KEY);
  if (saved !== null) {
    nav.scrollTop = parseInt(saved, 10) || 0;
  } else {
    var active = nav.querySelector('.sidebar-link.active');
    if (active) {
      var navRect = nav.getBoundingClientRect();
      var aRect = active.getBoundingClientRect();
      if (aRect.top < navRect.top || aRect.bottom > navRect.bottom) {
        active.scrollIntoView({ block: 'center' });
      }
    }
  }

  nav.addEventListener('scroll', function () {
    sessionStorage.setItem(KEY, String(nav.scrollTop));
  }, { passive: true });

  document.querySelectorAll('.sidebar-link').forEach(function (link) {
    link.addEventListener('click', function () {
      sessionStorage.setItem(KEY, String(nav.scrollTop));
    });
  });
})();
