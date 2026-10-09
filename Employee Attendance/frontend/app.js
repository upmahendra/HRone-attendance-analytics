const API_BASE = 'http://127.0.0.1:8000';
const monthFilter = document.getElementById('monthFilter');
const employeeForm = document.getElementById('employeeForm');
const employeesTableBody = document.getElementById('employeesTableBody');
const departmentSummaryBody = document.getElementById('departmentSummaryBody');
const totalEmployeesEl = document.getElementById('totalEmployees');
const departmentsCountEl = document.getElementById('departmentsCount');
const lateCountEl = document.getElementById('lateCount');
const avgHoursEl = document.getElementById('avgHours');
const toastEl = document.getElementById('toast');

function stringifyError(value) {
  if (value == null) return 'Request failed';
  if (typeof value === 'string') return value;
  if (value instanceof Error) return value.message || 'Request failed';
  if (Array.isArray(value)) return value.map((item) => stringifyError(item)).join(', ');
  if (typeof value === 'object') {
    const detail = value.detail;
    if (detail) return stringifyError(detail);
    return JSON.stringify(value);
  }
  return String(value);
}

function showToast(message, isError = false) {
  toastEl.textContent = String(message);
  toastEl.style.borderColor = isError ? 'rgba(255, 107, 124, 0.4)' : 'rgba(126, 240, 201, 0.25)';
  toastEl.classList.add('visible');
  setTimeout(() => toastEl.classList.remove('visible'), 2200);
}

async function apiRequest(path, { method = 'GET', body } = {}) {
  const options = {
    method,
    headers: {
      'Content-Type': 'application/json',
    },
  };

  if (body) {
    options.body = JSON.stringify(body);
  }

  const response = await fetch(`${API_BASE}${path}`, options);
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json() : await response.text();

  if (!response.ok) {
    const message = stringifyError(payload);
    throw new Error(message);
  }

  return payload;
}

function renderEmployees(items) {
  employeesTableBody.innerHTML = items.map((emp) => `
    <tr>
      <td>${emp.emp_code}</td>
      <td>${emp.name}</td>
      <td>${emp.department}</td>
      <td>${emp.shift_start} - ${emp.shift_end}</td>
      <td>${emp.joined_on}</td>
    </tr>
  `).join('');
}

function renderDepartmentSummary(items) {
  departmentSummaryBody.innerHTML = items.map((row) => `
    <tr>
      <td>${row.department}</td>
      <td>${row.headcount}</td>
      <td>${Number(row.present_days || 0).toFixed(1)}</td>
      <td>${row.avg_work_hours ?? '—'}</td>
      <td>${row.late_count}</td>
      <td>${row.total_late_minutes}</td>
    </tr>
  `).join('');
}

async function loadEmployees() {
  const data = await apiRequest('/employees?page=1&page_size=20');
  renderEmployees(data.items || []);
  totalEmployeesEl.textContent = data.total ?? 0;
}

async function loadDepartmentSummary() {
  const month = monthFilter.value;
  const data = await apiRequest(`/analytics/departments/summary?month=${month}`);
  const items = data.items || [];
  renderDepartmentSummary(items);

  const departmentCount = items.length;
  const totalLateCount = items.reduce((sum, row) => sum + (row.late_count || 0), 0);
  const validWorkHours = items.filter((row) => row.avg_work_hours !== null && row.avg_work_hours !== undefined);
  const avgHoursValue = validWorkHours.length
    ? validWorkHours.reduce((sum, row) => sum + Number(row.avg_work_hours), 0) / validWorkHours.length
    : 0;

  departmentsCountEl.textContent = departmentCount;
  lateCountEl.textContent = totalLateCount;
  avgHoursEl.textContent = avgHoursValue ? `${avgHoursValue.toFixed(2)}h` : '—';
}

async function refreshDashboard() {
  try {
    await Promise.all([loadEmployees(), loadDepartmentSummary()]);
  } catch (error) {
    showToast(error.message, true);
  }
}

employeeForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const formData = Object.fromEntries(new FormData(employeeForm).entries());

  try {
    await apiRequest('/employees', {
      method: 'POST',
      body: {
        ...formData,
      },
    });
    employeeForm.reset();
    showToast('Employee created');
    await refreshDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.getElementById('punchInBtn').addEventListener('click', async () => {
  const empCode = document.getElementById('punchEmpCode').value.trim();
  const status = document.getElementById('attendanceStatus').value;
  if (!empCode) {
    showToast('Enter employee code', true);
    return;
  }

  try {
    await apiRequest('/attendance/punch-in', {
      method: 'POST',
      body: {
        emp_code: empCode,
        punched_at: Date.now(),
        status,
      },
    });
    showToast('Punch in recorded');
    await refreshDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.getElementById('punchOutBtn').addEventListener('click', async () => {
  const empCode = document.getElementById('punchEmpCode').value.trim();
  if (!empCode) {
    showToast('Enter employee code', true);
    return;
  }

  try {
    await apiRequest('/attendance/punch-out', {
      method: 'POST',
      body: {
        emp_code: empCode,
        punched_at: Date.now(),
      },
    });
    showToast('Punch out recorded');
    await refreshDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
});

document.getElementById('refreshBtn').addEventListener('click', refreshDashboard);
monthFilter.addEventListener('change', loadDepartmentSummary);

window.addEventListener('DOMContentLoaded', () => {
  const today = new Date();
  const defaultMonth = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}`;
  monthFilter.value = defaultMonth;
  refreshDashboard();
});
