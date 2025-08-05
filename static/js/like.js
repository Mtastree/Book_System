document.addEventListener('DOMContentLoaded', function() {
    const likeButtons = document.querySelectorAll('.toggle-like-button');

    likeButtons.forEach(button => {
        button.addEventListener('click', function(event) {
            event.preventDefault();

            const reflectionId = this.dataset.reflectionId;
            const likeCountSpan = this.querySelector('.like-count');

            // 使用新的API端点
            fetch(`/toggle_like/${reflectionId}`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            })
            .then(response => {
                if (!response.ok) {
                    throw new Error('Network response was not ok');
                }
                return response.json();
            })
            .then(data => {
                if (data.success) {
                    // 更新点赞数
                    likeCountSpan.textContent = data.likes;

                    // 切换按钮样式
                    if (data.liked) {
                        this.classList.remove('btn-outline-danger');
                        this.classList.add('btn-danger');
                    } else {
                        this.classList.remove('btn-danger');
                        this.classList.add('btn-outline-danger');
                    }
                } else {
                    console.error('Failed to toggle like:', data.error);
                    alert('操作失败: ' + (data.error || '未知错误'));
                }
            })
            .catch(error => {
                console.error('Error:', error);
                alert('发生网络错误，请稍后再试。');
            });
        });
    });
});