/**
 * Chart.js Configuration and Real-Time Plotting
 * Manages 6 real-time sensor plots — reused from tomv3-web unchanged.
 */

const charts = {};

const chartConfig = {
    0:  { name: 'SGP40_VOC', color: 'rgb(75, 192, 192)' },
    1:  { name: 'SGP40_NOx', color: 'rgb(255, 159, 64)'  },
    2:  { name: 'BME_Res',   color: 'rgb(255, 99, 132)' },
    9:  { name: 'ENS_R2',    color: 'rgb(54, 162, 235)'  },
    10: { name: 'ENS_R3',    color: 'rgb(255, 206, 86)'  },
    11: { name: 'ENS_R4',    color: 'rgb(153, 102, 255)' },
};

function initializeCharts() {
    for (const [index, config] of Object.entries(chartConfig)) {
        const canvas = document.getElementById(`chart-${index}`);
        if (!canvas) continue;
        const ctx = canvas.getContext('2d');

        charts[index] = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [{
                    label: config.name,
                    data: [],
                    borderColor: config.color,
                    backgroundColor: config.color.replace('rgb', 'rgba').replace(')', ', 0.1)'),
                    borderWidth: 2,
                    pointRadius: 0,
                    tension: 0.1,
                    fill: true,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: true,
                animation: false,
                plugins: {
                    legend: { display: true, position: 'top' },
                    title: {
                        display: true,
                        text: config.name,
                        font: { size: 14, weight: 'bold' },
                    },
                },
                scales: {
                    x: { display: false, grid: { display: false } },
                    y: { beginAtZero: false, grid: { color: 'rgba(0,0,0,0.05)' } },
                },
                interaction: { intersect: false, mode: 'index' },
            },
        });
    }
    console.log('✓ Charts initialised');
}

function updateChart(channelIndex, newData) {
    const chart = charts[channelIndex];
    if (!chart) return;
    chart.data.datasets[0].data = newData;
    chart.data.labels = Array.from({ length: newData.length }, (_, i) => i);
    chart.update('none');
}

function handlePlotData(plotData) {
    for (const [channelIndex, data] of Object.entries(plotData)) {
        if (charts[channelIndex] && Array.isArray(data)) {
            updateChart(channelIndex, data);
        }
    }
}

function clearAllCharts() {
    for (const chart of Object.values(charts)) {
        chart.data.datasets[0].data = [];
        chart.data.labels = [];
        chart.update('none');
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeCharts);
} else {
    initializeCharts();
}

